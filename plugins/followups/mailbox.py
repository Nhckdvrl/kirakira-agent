"""Read-only IMAP collection. Protocol identities stay outside model decisions."""

from __future__ import annotations

import asyncio
import base64
import imaplib
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses

from .contract import MailboxConfig, WatchSpec
from .feeds import MAX_TEXT, Entry, _date, _plain, fingerprint

BATCH_SIZE = 10
BODY_BYTES = 65_536
HEADER_BYTES = 16_384


class MailboxError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def mailbox_name(value: str) -> str:
    """IMAP modified UTF-7, then quoted-string escaping; never a raw command."""

    def encoded(match):
        return (
            "&"
            + base64.b64encode(match[0].encode("utf-16-be"))
            .decode()
            .rstrip("=")
            .replace("/", ",")
            + "-"
        )

    value = re.sub(r"[^\x20-\x7e]+", encoded, value.replace("&", "&-"))
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def scrub(value: str) -> str:
    # Best-effort display reduction, not an authorization or complete DLP claim.
    value = re.sub(
        r"(?i)((?:verification|security|one.time|login)\s+code|验证码|校验码|动态码)(\s*[:：]?\s*)[A-Z0-9]{4,10}",
        r"\1\2[已隐藏]",
        value,
    )
    return re.sub(
        r"(?i)https?://[^\s<>\"']*(?:[?&](?:token|code|password|key)=|/(?:reset-password|magic-link)/)[^\s<>\"']*",
        "[登录凭据链接已隐藏]",
        value,
    )


def message(data: bytes, uid: int, size: int) -> dict:
    mail = BytesParser(policy=policy.default).parsebytes(data)
    body = mail.get_body(preferencelist=("plain", "html"))
    value = ""
    if body is not None:
        try:
            value = body.get_content()
        except LookupError, UnicodeError:
            value = (body.get_payload(decode=True) or b"").decode("utf-8", "replace")
        if body.get_content_type() == "text/html":
            value = _plain(value)
    references = re.findall(r"<[^<>\s]{1,998}>", str(mail.get("References", "")))
    parent = re.findall(r"<[^<>\s]{1,998}>", str(mail.get("In-Reply-To", "")))
    identity = str(mail.get("Message-ID", "")).strip()[:1000]
    value = scrub(str(value))
    return {
        "uid": uid,
        "message_id": identity,
        "thread_root": (references or parent or [identity or "uid:" + str(uid)])[0],
        "subject": scrub(str(mail.get("Subject", "")))[:1000],
        "from": [
            address.casefold() for _, address in getaddresses(mail.get_all("From", []))
        ],
        "date": _date(str(mail.get("Date", ""))),
        "text": value[:MAX_TEXT],
        "truncated": len(data) < size or len(value) > MAX_TEXT,
    }


def entries(messages: list[dict], account_id: str, folder: str) -> tuple[Entry, ...]:
    grouped = {}
    for row in messages:
        grouped.setdefault(row["thread_root"], []).append(row)
    result = []
    for thread, rows in grouped.items():
        unique = {
            fingerprint({k: v for k, v in row.items() if k != "uid"}): row
            for row in rows
        }
        rows = sorted(unique.values(), key=lambda row: row["uid"])
        pieces, presentation_truncated = [], False
        for index, row in reversed(list(enumerate(rows))):
            prefix = f"邮件接收次序 {index + 1}（最新先列；发件日期 {row['date'] or '未知'}）\n主题：{row['subject'][:200]}\n发件地址：{', '.join(row['from'])[:160]}\n"
            allowance = MAX_TEXT // len(rows)
            presentation_truncated |= len(prefix + row["text"]) > allowance
            pieces.append((prefix + row["text"])[:allowance])
        content = "\n\n".join(pieces)
        latest = rows[-1]
        identity = fingerprint([account_id, folder, thread])
        # UID is a transport watermark, not a semantic content revision.
        revision = fingerprint(
            [{k: v for k, v in row.items() if k != "uid"} for row in rows]
        )
        result.append(
            Entry(
                identity,
                revision,
                {
                    "title": latest["subject"],
                    "content": content[:MAX_TEXT],
                    "content_truncated": presentation_truncated
                    or len(content) > MAX_TEXT
                    or any(row["truncated"] for row in rows),
                    "url": None,
                    "published_at": latest["date"],
                    "mail": {
                        "account_id": account_id,
                        "folder": folder,
                        "thread_id": identity,
                        "message_ids": [row["message_id"] for row in rows],
                        "uids": [row["uid"] for row in rows],
                        "messages_in_update": len(rows),
                        "origin": "external-email; sender address is not authenticated owner authority",
                    },
                },
            )
        )
    return tuple(result)


@dataclass(frozen=True)
class Batch:
    entries: tuple[Entry, ...]
    cursor: dict
    scanned: int
    matched: int
    pending: int
    duplicates: int = 0


class _Connection:
    def __init__(self):
        self.lock = threading.Lock()
        self.client = None
        self.cancelled = False

    def attach(self, client):
        with self.lock:
            self.client = client
            if self.cancelled:
                client.shutdown()
                raise MailboxError("cancelled", "邮箱读取已取消")

    def abort(self):
        with self.lock:
            self.cancelled = True
            if self.client is not None:
                try:
                    self.client.socket().shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


class _ReadOnlyIMAP(imaplib.IMAP4_SSL):
    def read(self, size):
        if size > BODY_BYTES:
            raise MailboxError("message_too_large", "邮箱返回的内容超出约定读取范围")
        return super().read(size)


def _fetch(config, secret, spec, cursor, connection):
    deadline = time.monotonic() + 40
    client = None
    try:
        context = ssl.create_default_context()
        if config.tls_ca_file:
            context.load_verify_locations(cafile=config.tls_ca_file)
        client = _ReadOnlyIMAP(
            config.host, config.port, ssl_context=context, timeout=10
        )
        connection.attach(client)

        def command(method, *args):
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            client.socket().settimeout(min(10, left))
            status, result = method(*args)
            if status != "OK":
                raise MailboxError("protocol_error", "邮箱没有完成只读操作")
            return result

        if config.auth_method == "oauth2":
            auth = f"user={config.username}\x01auth=Bearer {secret}\x01\x01".encode()
            command(
                client.authenticate,
                "XOAUTH2",
                lambda challenge: auth if not challenge else b"",
            )
        else:
            command(client.login, config.username, secret)
        command(
            client.select, mailbox_name(spec.mail_folder), True
        )  # EXAMINE, never SELECT.

        def number(name):
            data = client.response(name)[1]
            if not data or not isinstance(data[0], bytes) or not data[0].isdigit():
                raise MailboxError("protocol_error", "邮箱未返回稳定的 UID 身份")
            return int(data[0])

        epoch, upper = number("UIDVALIDITY"), number("UIDNEXT") - 1
        if epoch <= 0 or upper < 0:
            raise MailboxError("protocol_error", "邮箱 UID 身份或水位无效")
        if not cursor.get("initialized"):
            return Batch((), {"uidvalidity": epoch, "last_uid": upper}, 0, 0, 0)
        previous = cursor.get("mailbox", {})
        if previous.get("uidvalidity") != epoch:
            raise MailboxError(
                "uidvalidity_changed",
                "邮箱身份已变化，已停止采集；请检查后重建基线，旧事件不会删除",
            )
        lower = int(previous["last_uid"])
        if upper < lower:
            raise MailboxError("uid_reversed", "邮箱 UID 水位倒退，已保留原基线")
        if lower == upper:
            return Batch((), previous, 0, 0, 0)
        found = command(client.uid, "SEARCH", None, "UID", f"{lower + 1}:{upper}")
        raw = b" ".join(value for value in found if isinstance(value, bytes))
        if len(raw) > 1_048_576:
            raise MailboxError(
                "backlog_too_large", "邮箱待处理索引过大，请缩小文件夹范围"
            )
        uids = sorted(
            {
                int(value)
                for value in raw.split()
                if value.isdigit() and lower < int(value) <= upper
            }
        )
        chosen, messages = uids[:BATCH_SIZE], []
        seen = list(previous.get("seen_messages", []))
        known, matched, duplicates = set(seen), 0, 0
        for uid in chosen:

            def fetch(section, cap, uid=uid):
                result = command(
                    client.uid,
                    "FETCH",
                    str(uid),
                    f"(UID RFC822.SIZE BODY.PEEK[{section}]<0.{cap}>)",
                )
                literal = next(
                    (
                        row
                        for row in result
                        if isinstance(row, tuple) and isinstance(row[1], bytes)
                    ),
                    None,
                )
                if literal is None:
                    # Expunged between SEARCH and FETCH: no body or invented event.
                    return None
                returned_uid = re.search(rb"\bUID (\d+)", literal[0])
                size = re.search(rb"\bRFC822.SIZE (\d+)", literal[0])
                if (
                    returned_uid is None
                    or int(returned_uid[1]) != uid
                    or size is None
                    or len(literal[1]) > cap
                ):
                    raise MailboxError("protocol_error", "邮箱 FETCH 身份或长度无效")
                return literal[1], int(size[1])

            header = fetch(
                "HEADER.FIELDS (MESSAGE-ID REFERENCES IN-REPLY-TO SUBJECT FROM DATE)",
                HEADER_BYTES,
            )
            if header is None:
                continue
            parsed = message(header[0], uid, header[1])
            if spec.mail_sender and spec.mail_sender.casefold() not in parsed["from"]:
                continue
            if (
                spec.mail_subject
                and spec.mail_subject.casefold() not in parsed["subject"].casefold()
            ):
                continue
            body = fetch("", BODY_BYTES)
            if body is not None:
                row = message(body[0], uid, body[1])
                matched += 1
                digest = fingerprint({k: v for k, v in row.items() if k != "uid"})
                if digest in known:
                    duplicates += 1
                else:
                    messages.append(row)
                    known.add(digest)
                    seen.append(digest)
        pending = max(0, len(uids) - len(chosen))
        watermark = chosen[-1] if pending else upper
        return Batch(
            entries(messages, config.account_id, spec.mail_folder),
            {
                "uidvalidity": epoch,
                "last_uid": watermark,
                "seen_messages": seen[-4096:],
            },
            len(chosen),
            matched,
            pending,
            duplicates,
        )
    except MailboxError:
        raise
    except imaplib.IMAP4.error:
        raise MailboxError(
            "imap_failed", "邮箱认证或只读操作失败，请检查账号、凭据与 IMAP 设置"
        ) from None
    except OSError, ssl.SSLError, TimeoutError:
        raise MailboxError(
            "connection_failed", "邮箱 TLS 连接失败或超时，请检查网络与证书"
        ) from None
    finally:
        if client is not None:
            try:
                client.shutdown()  # No CLOSE, STORE, EXPUNGE or SMTP side effect.
            except OSError:
                pass


async def collect(
    config: MailboxConfig, secret: str, spec: WatchSpec, cursor: dict
) -> Batch:
    connection = _Connection()
    work = asyncio.create_task(
        asyncio.to_thread(_fetch, config, secret, spec, cursor, connection)
    )
    try:
        async with asyncio.timeout(45):
            return await asyncio.shield(work)
    except (asyncio.CancelledError, TimeoutError) as error:
        connection.abort()
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                connection.abort()
            except Exception:  # noqa: BLE001 - drain the actual worker before releasing its credential lease
                break
        if not work.cancelled():
            work.exception()
        if isinstance(error, TimeoutError):
            raise MailboxError("timeout", "邮箱读取超时，已保留原水位") from None
        raise
