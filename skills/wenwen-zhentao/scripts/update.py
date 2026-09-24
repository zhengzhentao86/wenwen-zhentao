#!/usr/bin/env python3
"""问问镇涛更新器：仅 Python 3.9+ 标准库，无常驻任务、遥测或素材上传。

发布包须包含 release-files.json（自身之外全部文件的 SHA256）。远端清单
的 files 包含 release-files.json 自身。只接受安装时配置的 HTTPS 同源发布
地址；跨域 CDN 跳转不受支持。更新需要安装目录父目录及独立状态目录可写。
回滚包留在安装目录的同级隐藏目录，用户记录应保存在 state-dir 中。
"""

import argparse
import contextlib
import hashlib
import http.client
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import stat
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlsplit


SKILL = "wenwen-zhentao"
UPDATER_VERSION = 1
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_FILES = 2000
TIMEOUT = 10
SHA256 = re.compile(r"^[a-f0-9]{64}$")
VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class UpdateError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_object(data):
    """拒绝重复 JSON 键，避免校验者与消费者理解不一致。"""
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise UpdateError("invalid_metadata", "JSON 含重复字段。")
            result[key] = value
        return result
    try:
        result = json.loads(data, object_pairs_hook=unique_pairs)
    except (ValueError, UnicodeDecodeError) as exc:
        raise UpdateError("invalid_metadata", "元数据不是有效 JSON。") from exc
    if not isinstance(result, dict):
        raise UpdateError("invalid_metadata", "元数据必须为对象。")
    return result


def read_json(path, limit=MAX_MANIFEST_BYTES):
    if path.is_symlink():
        raise UpdateError("invalid_location", "元数据文件不能是符号链接。")
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("invalid_metadata", "元数据超过大小上限。")
    return json_object(data)


def atomic_json(path, value):
    if path.is_symlink():
        raise UpdateError("invalid_location", "状态文件不能是符号链接。")
    fd, temporary = tempfile.mkstemp(prefix=".update-json-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def version_tuple(value):
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise UpdateError("invalid_metadata", "版本号必须为不含前导零的 x.y.z。")
    return tuple(int(part) for part in value.split("."))


def metadata(skill_dir):
    value = read_json(skill_dir / "version.json")
    if value.get("schema_version") != 1 or value.get("skill") != SKILL:
        raise UpdateError("invalid_metadata", "安装包标识或元数据版本不匹配。")
    version_tuple(value.get("version"))
    return value


def valid_path(value):
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or "\x00" in value or any(ord(char) < 32 for char in value)):
        raise UpdateError("unsafe_archive", "包内路径无效。")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts) or PurePosixPath(value).is_absolute():
        raise UpdateError("unsafe_archive", "包内路径必须为无穿越的相对路径。")
    # 避免 Windows 设备名、尾随空格/句点及大小写冲突跨平台产生歧义。
    reserved = {"con", "prn", "aux", "nul"} | {"com" + str(n) for n in range(1, 10)} | {
        "lpt" + str(n) for n in range(1, 10)}
    if any(part.endswith((" ", ".")) or part.split(".")[0].lower() in reserved for part in parts):
        raise UpdateError("unsafe_archive", "包内路径不可移植。")
    return value


def file_map(value):
    if not isinstance(value, dict) or not value or len(value) > MAX_FILES:
        raise UpdateError("invalid_metadata", "文件清单缺失或过大。")
    folded = set()
    for path, expected in value.items():
        valid_path(path)
        if not isinstance(expected, str) or not SHA256.fullmatch(expected):
            raise UpdateError("invalid_metadata", "文件摘要必须是小写 SHA256。")
        if path.casefold() in folded:
            raise UpdateError("unsafe_archive", "文件名存在大小写冲突。")
        folded.add(path.casefold())
    return value


def inventory(skill_dir):
    result = {}
    size = 0
    for directory, subdirs, files in os.walk(str(skill_dir), followlinks=False):
        for name in subdirs + files:
            entry = Path(directory) / name
            if entry.is_symlink():
                raise UpdateError("local_changes", "安装目录含符号链接；保留现版。")
        for name in files:
            entry = Path(directory) / name
            if not stat.S_ISREG(entry.stat().st_mode):
                raise UpdateError("local_changes", "安装目录含非常规文件；保留现版。")
            relative = entry.relative_to(skill_dir).as_posix()
            if relative == "release-files.json":
                continue
            valid_path(relative)
            size += entry.stat().st_size
            if entry.stat().st_size > MAX_FILE_BYTES or size > MAX_EXPANDED_BYTES or len(result) >= MAX_FILES:
                raise UpdateError("local_changes", "本地目录超过发行包限制；保留现版。")
            result[relative] = digest(entry.read_bytes())
    return result


def verify_local(skill_dir):
    baseline_path = skill_dir / "release-files.json"
    if not baseline_path.exists():
        raise UpdateError("local_changes_unverified", "缺少发行文件基线，无法证明本地无改动；保留现版。")
    baseline = read_json(baseline_path)
    if baseline.get("schema_version") != 1:
        raise UpdateError("local_changes_unverified", "发行文件基线格式不受支持。")
    expected = file_map(baseline.get("files"))
    if "release-files.json" in expected:
        raise UpdateError("invalid_metadata", "本地基线不得包含自身。")
    if inventory(skill_dir) != expected:
        raise UpdateError("local_changes", "发现新增、删除或修改的本地文件；保留现版，请先保存改动。")


def valid_url(url, origin=None):
    if not isinstance(url, str) or any(ord(char) < 33 for char in url) or "\\" in url:
        raise UpdateError("untrusted_source", "发布地址无效。")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port or 443
    except ValueError as exc:
        raise UpdateError("untrusted_source", "发布地址无效。") from exc
    if (parsed.scheme != "https" or not host or parsed.username is not None
            or parsed.password is not None or parsed.fragment or port != 443):
        raise UpdateError("untrusted_source", "仅允许无凭据的 HTTPS 443 发布地址。")
    host = host.lower()
    if (host.endswith(".") or "." not in host or host == "localhost"
            or host.endswith((".localhost", ".local", ".internal"))):
        raise UpdateError("untrusted_source", "禁止本地或内部发布地址。")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise UpdateError("untrusted_source", "发布地址必须使用公开域名，不接受 IP 字面量。")
    try:
        host.encode("ascii")
    except UnicodeEncodeError as exc:
        raise UpdateError("untrusted_source", "发布地址须使用 ASCII/Punycode 域名。") from exc
    actual_origin = (host, port)
    if origin is not None and actual_origin != origin:
        raise UpdateError("untrusted_source", "发布清单、下载地址及跳转必须与已配发布源同源。")
    return parsed, actual_origin


def public_addresses(host, port):
    addresses = []
    for entry in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        address = entry[4][0]
        ip = ipaddress.ip_address(address)
        if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise UpdateError("untrusted_source", "发布源 DNS 指向内部或非公网地址。")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise OSError("发布源 DNS 无可用地址。")
    return addresses


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """DNS 解析后只连接已检查 IP，TLS 仍核对原域名，避免二次解析重绑定。"""
    def __init__(self, host, address):
        super().__init__(host, 443, timeout=TIMEOUT, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def fetch_https(url, max_bytes, origin):
    """GET 只携带固定客户端标识，不发送问题、记录、Cookie 或本机路径。"""
    for _ in range(4):
        parsed, _ = valid_url(url, origin)
        addresses = public_addresses(parsed.hostname, 443)
        connection = PinnedHTTPSConnection(parsed.hostname, addresses[0])
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            connection.request("GET", path, headers={"User-Agent": "wenwen-zhentao-updater/1",
                                                      "Accept-Encoding": "identity"})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    raise UpdateError("download_failed", "发布源跳转缺少目标地址。")
                url = urljoin(url, location)
                valid_url(url, origin)
                continue
            if response.status != 200:
                raise UpdateError("download_failed", "发布源返回 HTTP " + str(response.status) + "。")
            length = response.getheader("Content-Length")
            if length is not None:
                try:
                    advertised = int(length)
                except ValueError as exc:
                    raise UpdateError("download_failed", "下载大小字段无效。") from exc
                if advertised < 0 or advertised > max_bytes:
                    raise UpdateError("download_too_large", "下载超过大小上限。")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise UpdateError("download_failed", "发布源必须提供未经 HTTP 压缩的文件。")
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise UpdateError("download_too_large", "下载超过大小上限。")
            if length is not None and len(data) != advertised:
                raise UpdateError("download_failed", "下载未完成。")
            return data
        finally:
            connection.close()
    raise UpdateError("download_failed", "发布源跳转次数过多。")


@contextlib.contextmanager
def install_lock(skill_dir):
    """使用同安装目录旁的独占锁；更换 state-dir 不能绕过并发保护。

    正常结束自动释放。进程被强制杀死可能留下锁；不自动猜测并删除，需在
    确认没有更新进程后手动移除错误信息中指定的锁文件，再重试恢复事务。
    """
    lock = skill_dir.parent / ("." + skill_dir.name + ".update.lock")
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise UpdateError("busy", "存在更新锁；确认无更新进程后再处理：" + str(lock)) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "created_at": int(time.time())}, handle)
        yield
    finally:
        lock.unlink(missing_ok=True)


def locations(skill_dir, state_dir):
    skill_dir = Path(os.path.abspath(str(skill_dir)))
    state_dir = Path(os.path.abspath(str(state_dir)))
    if skill_dir.is_symlink() or state_dir.is_symlink():
        raise UpdateError("invalid_location", "Skill 目录必须为真实存在的目录，不能是符号链接。")
    # macOS 的 /var、/tmp 本身是系统别名；规范化父路径，同时拒绝目标目录链接。
    skill_dir = skill_dir.parent.resolve() / skill_dir.name
    state_dir = state_dir.parent.resolve() / state_dir.name
    if not skill_dir.is_dir() and not (state_dir / "transaction.json").is_file():
        raise UpdateError("invalid_location", "Skill 安装目录不存在且没有恢复事务。")
    if state_dir == skill_dir or skill_dir in state_dir.parents or state_dir in skill_dir.parents:
        raise UpdateError("invalid_location", "状态目录须与 Skill 安装目录分离，不能互相包含。")
    return skill_dir, state_dir


def checked_work_path(value, skill_dir, prefix):
    path = Path(value)
    if (path.parent != skill_dir.parent or not path.name.startswith("." + skill_dir.name + prefix)
            or path.is_symlink()):
        raise UpdateError("recovery_required", "恢复记录中的目录不属于本 Skill。")
    return path


def recover(skill_dir, state_dir):
    journal = state_dir / "transaction.json"
    if not journal.exists():
        return
    record = read_json(journal)
    if record.get("skill_dir") != str(skill_dir):
        raise UpdateError("recovery_required", "状态目录属于其他安装，不执行恢复。")
    backup = checked_work_path(record["backup"], skill_dir, ".backup-")
    stage = checked_work_path(record["stage"], skill_dir, ".stage-")
    if backup.exists():
        verify_local(backup)
        if skill_dir.exists():
            verify_local(skill_dir)
            if metadata(skill_dir)["version"] != record["next_version"]:
                raise UpdateError("recovery_required", "中断后的安装版本不符合记录，需人工核对。")
            # 已成功切换且状态已提交，只需清理事务记录。
            receipt = state_dir / "installation.json"
            if receipt.exists() and read_json(receipt).get("backup") == str(backup):
                journal.unlink()
                return
            temporary = skill_dir.parent / ("." + skill_dir.name + ".stage-" + uuid.uuid4().hex)
            os.replace(str(skill_dir), str(temporary))
            try:
                os.replace(str(backup), str(skill_dir))
            except Exception:
                os.replace(str(temporary), str(skill_dir))
                raise
            shutil.rmtree(temporary)
        else:
            os.replace(str(backup), str(skill_dir))
    elif not skill_dir.exists():
        raise UpdateError("recovery_required", "安装目录与恢复备份均缺失。")
    if stage.exists():
        shutil.rmtree(stage)
    journal.unlink()


def manifest_data(data, origin):
    value = json_object(data)
    if value.get("schema_version") != 1 or value.get("skill") != SKILL:
        raise UpdateError("invalid_manifest", "远端清单格式或 Skill 标识不匹配。")
    version_tuple(value.get("version"))
    minimum = value.get("min_updater_version")
    if type(minimum) is not int or minimum < 1:
        raise UpdateError("invalid_manifest", "缺少有效的最低更新器版本。")
    if minimum > UPDATER_VERSION:
        raise UpdateError("incompatible", "新版本需要更高版本更新器；保留现版，请按正式安装说明升级。")
    valid_url(value.get("archive_url"), origin)
    if not isinstance(value.get("sha256"), str) or not SHA256.fullmatch(value["sha256"]):
        raise UpdateError("invalid_manifest", "缺少有效的 ZIP 摘要。")
    file_map(value.get("files"))
    if not {"SKILL.md", "version.json", "release-files.json"}.issubset(value["files"]):
        raise UpdateError("invalid_manifest", "发布清单缺少必需文件。")
    return value


def unpack(data, stage, manifest, original_metadata, origin):
    if len(data) > MAX_ARCHIVE_BYTES or digest(data) != manifest["sha256"]:
        raise UpdateError("integrity_error", "ZIP 摘要与发布清单不一致。")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UpdateError("unsafe_archive", "发行包不是有效 ZIP。") from exc
    with archive:
        entries = archive.infolist()
        if len(entries) > MAX_FILES * 2:
            raise UpdateError("unsafe_archive", "ZIP 条目过多。")
        names, folded, total = set(), set(), 0
        for entry in entries:
            name = entry.filename
            if entry.is_dir():
                name = name[:-1]
            valid_path(name)
            if name != SKILL and not name.startswith(SKILL + "/"):
                raise UpdateError("unsafe_archive", "ZIP 必须且只能包含 wenwen-zhentao 根目录。")
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise UpdateError("unsafe_archive", "ZIP 不允许符号链接或特殊文件。")
            if entry.flag_bits & 1:
                raise UpdateError("unsafe_archive", "ZIP 不允许加密文件。")
            if entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise UpdateError("unsafe_archive", "ZIP 压缩方式不受支持。")
            if name.casefold() in folded:
                raise UpdateError("unsafe_archive", "ZIP 含重复或大小写冲突路径。")
            folded.add(name.casefold())
            if entry.is_dir():
                continue
            if name == SKILL:
                raise UpdateError("unsafe_archive", "ZIP 根目录不是目录。")
            relative = name[len(SKILL) + 1:]
            total += entry.file_size
            if (entry.file_size > MAX_FILE_BYTES or total > MAX_EXPANDED_BYTES
                    or (entry.file_size > 1024 * 1024 and entry.file_size > max(entry.compress_size, 1) * 200)):
                raise UpdateError("unsafe_archive", "ZIP 解压大小或压缩比超过上限。")
            if relative not in manifest["files"]:
                raise UpdateError("integrity_error", "ZIP 含未声明文件。")
            try:
                with archive.open(entry) as handle:
                    content = handle.read(MAX_FILE_BYTES + 1)
            except (zipfile.BadZipFile, RuntimeError, EOFError) as exc:
                raise UpdateError("integrity_error", "ZIP 文件损坏。") from exc
            if len(content) != entry.file_size or digest(content) != manifest["files"][relative]:
                raise UpdateError("integrity_error", "包内文件摘要不一致。")
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            names.add(relative)
        if names != set(manifest["files"]):
            raise UpdateError("integrity_error", "ZIP 缺少发布清单中的文件。")
    installed = metadata(stage)
    if installed["version"] != manifest["version"]:
        raise UpdateError("invalid_package", "包内版本与发布清单不一致。")
    # 远端包不能悄悄改变未来信任源或关闭更新。
    if installed.get("update_manifest_url") != original_metadata.get("update_manifest_url"):
        raise UpdateError("invalid_package", "远端包改变了安装时配置的发布源。")
    valid_url(installed["update_manifest_url"], origin)
    try:
        verify_local(stage)
    except UpdateError as exc:
        raise UpdateError("invalid_package", "发行包内的文件基线无效。") from exc


def result(status, installed=None, detail=None, **extra):
    value = {"status": status, "skill": SKILL, "installed_version": installed,
             "loaded_version": None, "reload_required": False}
    if detail:
        value["detail"] = detail
    value.update(extra)
    return value


def failure(exc, skill_dir):
    current = None
    try:
        current = metadata(Path(skill_dir))["version"]
    except (OSError, UpdateError):
        pass
    if isinstance(exc, UpdateError):
        return result(exc.status, current, str(exc))
    if isinstance(exc, PermissionError):
        return result("permission_denied", current, "无目录写权限；保留现版，不扩大系统权限。")
    return result("offline_or_unreachable", current, "网络或本地文件操作失败；未确认安装新版，请继续使用现版。")


def activate(skill_dir, state_dir, stage, old_version, next_version):
    backup = skill_dir.parent / ("." + skill_dir.name + ".backup-" + uuid.uuid4().hex)
    record = {"schema_version": 1, "skill_dir": str(skill_dir), "backup": str(backup),
              "stage": str(stage), "previous_version": old_version, "next_version": next_version}
    journal = state_dir / "transaction.json"
    atomic_json(journal, record)
    moved_old = False
    moved_new = False
    try:
        os.replace(str(skill_dir), str(backup))
        moved_old = True
        os.replace(str(stage), str(skill_dir))
        moved_new = True
        atomic_json(state_dir / "installation.json", record)
    except Exception:
        if moved_old:
            if moved_new:
                os.replace(str(skill_dir), str(stage))
            try:
                os.replace(str(backup), str(skill_dir))
            except Exception as exc:
                raise UpdateError("recovery_required", "切换失败且恢复未完成；原版保存在同级备份目录，下次运行将恢复。") from exc
        journal.unlink(missing_ok=True)
        raise
    journal.unlink(missing_ok=True)


def check(skill_dir, state_dir, apply=False, manifest_url=None, fetch=None):
    """公开 Python 接口；fetch 注入仅供离线测试，CLI 始终使用真实安全传输。"""
    fetch = fetch or fetch_https
    stage = None
    try:
        skill_dir, state_dir = locations(skill_dir, state_dir)
        with install_lock(skill_dir):
            recover(skill_dir, state_dir)
            installed = metadata(skill_dir)
            current = installed["version"]
            configured = installed.get("update_manifest_url")
            if not configured:
                return result("not_configured", current, "发行地址尚未配置，沿用现版。")
            _, origin = valid_url(configured)
            url = manifest_url or configured
            valid_url(url, origin)
            remote = manifest_data(fetch(url, MAX_MANIFEST_BYTES, origin), origin)
            if version_tuple(remote["version"]) <= version_tuple(current):
                status = "up_to_date" if remote["version"] == current else "remote_older"
                return result(status, current, available_version=remote["version"])
            if not apply:
                return result("update_available", current, available_version=remote["version"])
            verify_local(skill_dir)
            state_dir.mkdir(parents=True, exist_ok=True)
            # 状态目录可写测试放在下载、切换之前。
            with tempfile.TemporaryFile(dir=str(state_dir)):
                pass
            stage = Path(tempfile.mkdtemp(prefix="." + skill_dir.name + ".stage-", dir=str(skill_dir.parent)))
            unpack(fetch(remote["archive_url"], MAX_ARCHIVE_BYTES, origin), stage, remote, installed, origin)
            verify_local(skill_dir)
            activate(skill_dir, state_dir, stage, current, remote["version"])
            return result("installed", remote["version"],
                          "文件已安装；宿主须重新读取 Skill 与知识文件，或开始新会话后确认加载。",
                          previous_version=current, reload_required=True)
    except (UpdateError, OSError, http.client.HTTPException, KeyError, TypeError) as exc:
        return failure(exc, skill_dir)
    finally:
        if stage is not None and stage.exists():
            # 出现 recovery_required 时保留事务涉及的文件，避免损坏恢复路径。
            if not (Path(state_dir) / "transaction.json").exists():
                shutil.rmtree(stage)


def rollback(skill_dir, state_dir):
    try:
        skill_dir, state_dir = locations(skill_dir, state_dir)
        with install_lock(skill_dir):
            recover(skill_dir, state_dir)
            receipt_path = state_dir / "installation.json"
            if not receipt_path.exists():
                return result("no_rollback", metadata(skill_dir)["version"], "没有可用的更新备份。")
            receipt = read_json(receipt_path)
            if receipt.get("skill_dir") != str(skill_dir):
                raise UpdateError("invalid_location", "恢复记录属于其他安装。")
            backup = checked_work_path(receipt["backup"], skill_dir, ".backup-")
            if not backup.is_dir():
                raise UpdateError("no_rollback", "更新备份已不存在。")
            verify_local(skill_dir)
            verify_local(backup)
            current = metadata(skill_dir)["version"]
            previous = metadata(backup)["version"]
            # 复用事务切换；先把备份移动为暂存目录，失败时放回原位。
            stage = skill_dir.parent / ("." + skill_dir.name + ".stage-" + uuid.uuid4().hex)
            os.replace(str(backup), str(stage))
            try:
                activate(skill_dir, state_dir, stage, current, previous)
            except Exception:
                if stage.exists() and not backup.exists() and not (state_dir / "transaction.json").exists():
                    os.replace(str(stage), str(backup))
                raise
            return result("rolled_back", previous, "文件已回退；需让宿主重新读取对应版本。",
                          previous_version=current, reload_required=True)
    except (UpdateError, OSError, KeyError, TypeError) as exc:
        return failure(exc, skill_dir)


def main(argv=None):
    parser = argparse.ArgumentParser(description="问问镇涛 Skill 同源安全更新器")
    parser.add_argument("--skill-dir", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    check_parser = commands.add_parser("check")
    check_parser.add_argument("--apply", action="store_true")
    check_parser.add_argument("--manifest-url", help="仅允许已配置发布源的同源地址")
    commands.add_parser("rollback")
    args = parser.parse_args(argv)
    if args.command == "check":
        outcome = check(args.skill_dir, args.state_dir, args.apply, args.manifest_url)
    else:
        outcome = rollback(args.skill_dir, args.state_dir)
    print(json.dumps(outcome, ensure_ascii=False, sort_keys=True))
    return 0 if outcome["status"] in {"not_configured", "up_to_date", "remote_older",
                                      "update_available", "installed", "rolled_back", "no_rollback"} else 1


if __name__ == "__main__":
    sys.exit(main())
