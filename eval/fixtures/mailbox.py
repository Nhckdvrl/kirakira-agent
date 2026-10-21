"""Real local TLS IMAP fixture; no outside account or SMTP traffic."""

import asyncio
import base64
import ipaddress
import os
import re
import ssl
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


class MailboxServer:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.username, self.password = "owner@example.test", uuid4().hex
        self.uidvalidity, self.uidnext = 71, 1
        self.messages, self.commands, self.connections = {}, [], set()
        self.hold, self.fetch_started = asyncio.Event(), asyncio.Event()
        self.hold.set()

    def deliver(
        self,
        subject,
        text,
        *,
        sender="updates@example.test",
        message_id=None,
        thread=None,
    ):
        message = EmailMessage(policy=policy.SMTP)
        identity = message_id or "<" + uuid4().hex + "@example.test>"
        message["Message-ID"] = identity
        message["Subject"], message["From"], message["To"] = (
            subject,
            sender,
            self.username,
        )
        message["Date"] = format_datetime(datetime.now(UTC))
        if thread:
            message["References"] = thread
            message["In-Reply-To"] = thread.split()[-1]
        message.set_content(text)
        uid = self.uidnext
        self.uidnext += 1
        self.messages[uid] = message.as_bytes()
        return uid, identity

    def certificate(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.now(UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.DNSName("localhost"),
                        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    ]
                ),
                critical=False,
            )
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=None), critical=True
            )
            .sign(key, hashes.SHA256())
        )
        self.ca_file = self.directory / "local-ca.pem"
        self.ca_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_file = self.directory / "local-key.pem"
        key_file.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        os.chmod(key_file, 0o600)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.ca_file, key_file)
        return context

    async def __aenter__(self):
        self.server = await asyncio.start_server(
            self.handle, "127.0.0.1", 0, ssl=self.certificate()
        )
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *args):
        self.hold.set()
        self.server.close()
        await self.server.wait_closed()
        writers = tuple(self.connections)
        for writer in writers:
            writer.close()
        await asyncio.gather(
            *(writer.wait_closed() for writer in writers), return_exceptions=True
        )

    def config(self):
        return {
            "enabled": True,
            "account_id": "ma_" + "a" * 32,
            "host": "127.0.0.1",
            "port": self.port,
            "username": self.username,
            "tls_ca_file": str(self.ca_file),
        }

    async def handle(self, reader, writer):
        self.connections.add(writer)
        authenticated = False
        try:
            writer.write(b"* OK Local evaluation mailbox\r\n")
            await writer.drain()
            while line := await reader.readline():
                tag, command = line.decode().strip().split(" ", 1)
                upper = command.upper()
                self.commands.append(
                    {
                        "command": upper.split()[0]
                        if not upper.startswith("UID ")
                        else "UID " + upper.split()[1],
                        "peek": "BODY.PEEK[" in upper,
                    }
                )
                if upper == "CAPABILITY":
                    writer.write(b"* CAPABILITY IMAP4rev1 AUTH=XOAUTH2\r\n")
                elif upper == "AUTHENTICATE XOAUTH2":
                    writer.write(b"+ \r\n")
                    await writer.drain()
                    auth = base64.b64decode((await reader.readline()).strip()).decode()
                    if (
                        f"user={self.username}\x01auth=Bearer {self.password}\x01\x01"
                        != auth
                    ):
                        writer.write(f"{tag} NO Invalid credentials\r\n".encode())
                        await writer.drain()
                        continue
                    authenticated = True
                elif upper.startswith("LOGIN "):
                    if self.username not in command or self.password not in command:
                        writer.write(f"{tag} NO Invalid credentials\r\n".encode())
                        await writer.drain()
                        continue
                    authenticated = True
                elif not authenticated:
                    writer.write(f"{tag} NO Authenticate first\r\n".encode())
                    await writer.drain()
                    continue
                elif upper.startswith("EXAMINE "):
                    writer.write(
                        f"* {len(self.messages)} EXISTS\r\n* FLAGS (\\Seen)\r\n* OK [UIDVALIDITY {self.uidvalidity}] stable\r\n* OK [UIDNEXT {self.uidnext}] next\r\n".encode()
                    )
                elif upper.startswith("UID SEARCH "):
                    lower, high = (
                        int(value)
                        for value in re.search(r"(\d+):(\d+)$", command).groups()
                    )
                    writer.write(
                        b"* SEARCH "
                        + b" ".join(
                            str(uid).encode()
                            for uid in sorted(self.messages)
                            if lower <= uid <= high
                        )
                        + b"\r\n"
                    )
                elif upper.startswith("UID FETCH "):
                    self.fetch_started.set()
                    await self.hold.wait()
                    uid = int(command.split()[2])
                    original = self.messages.get(uid)
                    if original is not None:
                        cap = int(re.search(r"<0\.(\d+)>", command)[1])
                        value = (
                            original.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                            if "HEADER.FIELDS" in upper
                            else original
                        )
                        value = value[:cap]
                        writer.write(
                            f"* 1 FETCH (UID {uid} RFC822.SIZE {len(original)} BODY[]<0> {{{len(value)}}}\r\n".encode()
                            + value
                            + b")\r\n"
                        )
                else:
                    writer.write(f"{tag} BAD Unsupported operation\r\n".encode())
                    await writer.drain()
                    continue
                writer.write(f"{tag} OK Done\r\n".encode())
                await writer.drain()
        except ConnectionError, ssl.SSLError, ValueError:
            pass
        finally:
            self.connections.discard(writer)
            writer.close()
