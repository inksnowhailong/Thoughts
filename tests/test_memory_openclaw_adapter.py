from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from guanjia.memory.contracts import MemoryQuery
from guanjia.memory.openclaw_adapter import OpenClawMemoryPath, query_to_dto


def test_openclaw_mapping_is_pure_and_workspace_free() -> None:
    path = OpenClawMemoryPath.from_logical_key('person/wife')
    assert path.relative_path == 'memory/people/wife.md'
    dto = query_to_dto(MemoryQuery(text='allergy', people=('wife',), limit=3))
    assert dto == {'query': 'allergy', 'people': ['wife'], 'limit': 3}


def test_openclaw_mapping_rejects_unsafe_paths() -> None:
    for unsafe in ('/etc/passwd', 'person/../wife', 'person//wife', 'C:/temp', 'person\\wife', 'person/\x00wife'):
        try:
            OpenClawMemoryPath.from_logical_key(unsafe)
        except ValueError:
            continue
        raise AssertionError(f'unsafe path accepted: {unsafe!r}')
