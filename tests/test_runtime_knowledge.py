import json
import tempfile
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from test_build import fixture
from build import build
from wwzt_core import runtime_item, validate_catalog, write_json, fingerprint
from install_local import install, replace_local
import maintain


class RuntimeKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / 'source'
        fixture(self.root)
        self.catalog = json.loads((self.root / 'knowledge/catalog.json').read_text())
        self.catalog = {'schema_version': 2, 'items': [runtime_item(x) for x in self.catalog['items']]}
        write_json(self.root / 'knowledge/catalog.json', self.catalog)

    def tearDown(self):
        self.tmp.cleanup()

    def test_runtime_catalog_cannot_reintroduce_source_fields(self):
        validate_catalog(self.root)
        self.catalog['items'][0]['source_label'] = '私人课程名'
        write_json(self.root / 'knowledge/catalog.json', self.catalog)
        with self.assertRaisesRegex(ValueError, '非运行字段'):
            validate_catalog(self.root)

    def test_runtime_cards_cannot_include_provenance_sections(self):
        p = self.root / 'knowledge/WWZT-001.md'
        p.write_text(p.read_text() + '\n## 来源说明\n私人课程与原文定位')
        with self.assertRaisesRegex(ValueError, '来源或维护'):
            validate_catalog(self.root)

    def test_local_merge_projects_metadata_without_losing_private_evidence(self):
        ws = self.base / 'maintenance'
        source = self.base / 'evidence.txt'
        source.write_text('Q2：先核对工具版本和报错文字，再针对当前步骤修正。')
        sid = maintain.manual_local(source, ws)['source_id']
        from test_maintain import item
        value = item(2)
        candidate = self.base / 'candidate.json'
        write_json(candidate, {'items': [{'item': value,
            'content': '# 新问题\n\n先核对工具版本和报错文字，再针对当前步骤修正。检查修改后原来的错误是否消失；信息不足时继续核对。',
            'evidence': {'source_id': sid, 'anchor': 'Q2', 'quote': '先核对工具版本和报错文字，再针对当前步骤修正。'}}]})
        staged = maintain.stage(candidate, self.root, ws)
        maintain.approve(staged['batch'], staged['review_digest'], '测试确认', 'all', True, ws)
        maintain.apply(staged['batch'], '0.2.0', self.root, ws)
        result = validate_catalog(self.root)
        self.assertEqual(2, len(result['items']))
        self.assertNotIn('source_label', result['items'][1])
        saved = json.loads((ws / 'batches' / staged['batch'] / 'candidate.json').read_text())
        self.assertEqual(sid, saved['items'][0]['evidence']['source_id'])

    def test_replace_keeps_backup_and_rejects_unreviewed_local_changes(self):
        built = build(self.root, self.base / 'out')
        manifest = self.base / 'out/preview-manifest.json'
        target = self.base / 'installed'
        install(built['archive'], manifest, target)
        old_fp = fingerprint(target)[0]
        (target / 'SKILL.md').write_text('用户的新修改')
        with self.assertRaisesRegex(ValueError, '新改动'):
            replace_local(built['archive'], manifest, target, old_fp, self.base / 'state')
        self.assertEqual('用户的新修改', (target / 'SKILL.md').read_text())
        current_fp = fingerprint(target)[0]
        result = replace_local(built['archive'], manifest, target, current_fp, self.base / 'state')
        self.assertEqual(current_fp, fingerprint(Path(result['backup']))[0])
        self.assertEqual(2, validate_catalog(target)['schema_version'])
