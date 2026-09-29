#!/usr/bin/env python3
"""匿名下载正式安装包，在空目录校验并解压；不触碰用户安装。"""
import hashlib
import json
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from build_entry_bundle import verify_bundle

BASE = 'https://github.com/zhengzhentao86/wenwen-zhentao/releases/latest/download/'


def download(name):
    request = urllib.request.Request(BASE + name, headers={'User-Agent': 'wenwen-zhentao-install-check'})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(20 * 1024 * 1024 + 1)
    if len(data) > 20 * 1024 * 1024:
        raise ValueError('下载超过大小限制')
    return data


def main():
    with tempfile.TemporaryDirectory(prefix='wwzt-public-install-') as directory:
        root = Path(directory)
        archive = root / 'install.zip'
        manifest = json.loads(download('wenwen-zhentao-install-manifest.json'))
        archive.write_bytes(download('wenwen-zhentao-install.zip'))
        result = verify_bundle(archive, manifest)
        installed = root / 'skills'
        with zipfile.ZipFile(archive) as package:
            package.extractall(installed)  # verify_bundle 已逐项验证安全路径和哈希
        for name in ('zt/SKILL.md', 'wenwen-zhentao/SKILL.md',
                     'wenwen-zhentao/references/welcome.md'):
            if not (installed / name).is_file():
                raise ValueError('缺少安装入口：' + name)
        for name, expected in manifest['files'].items():
            if hashlib.sha256((installed / name).read_bytes()).hexdigest() != expected:
                raise ValueError('解压后文件校验失败：' + name)
        print(json.dumps(dict(result, version=manifest['version'], anonymous=True,
                              fresh_install=True), ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except urllib.error.HTTPError as error:
        raise SystemExit(f'下载失败：HTTP {error.code}；访问失败不代表仓库为空。')
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        raise SystemExit(f'安装验收失败：{error}')
