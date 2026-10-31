"""Real local TLS IMAP fixture; no outside account or SMTP traffic."""

import asyncio
import base64
import ipaddress
import os
import re
import ssl
from dataclasses import dataclass, field
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


@dataclass
class MailboxState:
    uidvalidity: int
    uidnext: int = 1
    messages: dict[int, bytes] = field(default_factory=dict)


def _mailbox_name(value):
    """Decode a quoted modified UTF-7 mailbox without accepting raw commands."""
    if value.startswith('"') and value.endswith('"'):
        value = re.sub(r"\\(.)", r"\1", value[1:-1])

    def decoded(match):
        token = match[1]
        if not token:
            return "&"
        encoded = token.replace(",", "/")
        encoded += "=" * (-len(encoded) % 4)
        return base64.b64decode(encoded, validate=True).decode("utf-16-be")

    value = re.sub(r"&([A-Za-z0-9+,]*)-", decoded, value)
    return "INBOX" if value.casefold() == "inbox" else value


class MailboxServer:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.username, self.password = "owner@example.test", uuid4().hex
        self.folders = {"INBOX": MailboxState(uidvalidity=71)}
        self.commands, self.connections = [], set()
        self.hold, self.fetch_started = asyncio.Event(), asyncio.Event()
        self.hold.set()

    def create_folder(self, name, *, uidvalidity=None):
        name = "INBOX" if name.casefold() == "inbox" else name
        if name not in self.folders:
            self.folders[name] = MailboxState(
                uidvalidity=uidvalidity
                if uidvalidity is not None
                else 71 + len(self.folders)
            )
        return self.folders[name]

    @property
    def messages(self):
        return self.folders["INBOX"].messages

    @messages.setter
    def messages(self, value):
        self.folders["INBOX"].messages = value

    @property
    def uidvalidity(self):
        return self.folders["INBOX"].uidvalidity

    @uidvalidity.setter
    def uidvalidity(self, value):
        self.folders["INBOX"].uidvalidity = value

    @property
    def uidnext(self):
        return self.folders["INBOX"].uidnext

    @uidnext.setter
    def uidnext(self, value):
        self.folders["INBOX"].uidnext = value

    def deliver(
        self,
        subject,
        text,
        *,
        sender="updates@example.test",
        message_id=None,
        thread=None,
        folder="INBOX",
        date=None,
    ):
        message = EmailMessage(policy=policy.SMTP)
        identity = message_id or "<" + uuid4().hex + "@example.test>"
        message["Message-ID"] = identity
        message["Subject"], message["From"], message["To"] = (
            subject,
            sender,
            self.username,
        )
        message["Date"] = (
            date
            if isinstance(date, str)
            else format_datetime(date or datetime.now(UTC))
        )
        if thread:
            message["References"] = thread
            message["In-Reply-To"] = thread.split()[-1]
        message.set_content(text)
        mailbox = self.create_folder(folder)
        uid = mailbox.uidnext
        mailbox.uidnext += 1
        mailbox.messages[uid] = message.as_bytes()
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
        selected = None
        try:
            writer.write(b"* OK Local evaluation mailbox\r\n")
            await writer.drain()
            while line := await reader.readline():
                tag, command = line.decode().strip().split(" ", 1)
                upper = command.upper()
                observed = {
                    "command": upper.split()[0]
                    if not upper.startswith("UID ")
                    else "UID " + upper.split()[1],
                    "peek": "BODY.PEEK[" in upper,
                    "folder": selected,
                }
                self.commands.append(observed)
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
                    folder = _mailbox_name(command.partition(" ")[2])
                    observed["folder"] = folder
                    selected = None
                    if folder not in self.folders:
                        writer.write(f"{tag} NO Mailbox does not exist\r\n".encode())
                        await writer.drain()
                        continue
                    selected = folder
                    mailbox = self.folders[selected]
                    writer.write(
                        f"* {len(mailbox.messages)} EXISTS\r\n* FLAGS (\\Seen)\r\n* OK [UIDVALIDITY {mailbox.uidvalidity}] stable\r\n* OK [UIDNEXT {mailbox.uidnext}] next\r\n".encode()
                    )
                elif upper.startswith("UID SEARCH "):
                    if selected is None:
                        writer.write(f"{tag} NO Select a mailbox first\r\n".encode())
                        await writer.drain()
                        continue
                    mailbox = self.folders[selected]
                    lower, high = (
                        int(value)
                        for value in re.search(r"(\d+):(\d+)$", command).groups()
                    )
                    writer.write(
                        b"* SEARCH "
                        + b" ".join(
                            str(uid).encode()
                            for uid in sorted(mailbox.messages)
                            if lower <= uid <= high
                        )
                        + b"\r\n"
                    )
                elif upper.startswith("UID FETCH "):
                    if selected is None:
                        writer.write(f"{tag} NO Select a mailbox first\r\n".encode())
                        await writer.drain()
                        continue
                    mailbox = self.folders[selected]
                    self.fetch_started.set()
                    await self.hold.wait()
                    uid = int(command.split()[2])
                    observed.update(uid=uid, headers="HEADER.FIELDS" in upper)
                    original = mailbox.messages.get(uid)
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
