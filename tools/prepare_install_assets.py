#!/usr/bin/env python3
"""校验正式完整包，生成每个 Release 都必须提供的固定名称附件。"""
import argparse
import shutil
from pathlib import Path
from build_entry_bundle import verify_bundle
from wwzt_core import read_json


def prepare(out, version):
    archive = out / f'zt-{version}.zip'
    manifest = out / f'zt-{version}-manifest.json'
    metadata = read_json(manifest)
    if metadata['channel'] != 'stable' or metadata['version'] != version:
        raise ValueError('必须提供版本一致的正式包')
    verify_bundle(archive, metadata)
    shutil.copy2(archive, out / 'wenwen-zhentao-install.zip')
    shutil.copy2(manifest, out / 'wenwen-zhentao-install-manifest.json')
    verify_bundle(out / 'wenwen-zhentao-install.zip', out / 'wenwen-zhentao-install-manifest.json')
    print('固定名称安装包和清单已生成并校验。')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=Path('dist'))
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    prepare(args.out, args.version)
