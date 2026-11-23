from pathlib import Path

import pytest
import yaml

from watchpoint.config import ConfigError, Target, load_config


def _write(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_misspelt_key_is_reported(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "t.yaml", {"targets": [{"name": "q", "source": "a.xlsx", "wehre": {"回答者": "佐藤"}}]})
    with pytest.raises(ConfigError) as e:
        load_config(cfg)
    assert "wehre" in str(e.value) and "知らない項目" in str(e.value) and "1件目（q）" in str(e.value)


def test_vars_are_substituted_and_unknown_vars_are_errors(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path / "t.yaml",
        {
            "vars": {"me": "佐藤"},
            "targets": [{"name": "q", "source": "a.xlsx", "key": "No.", "where": {"回答者": "${me}"}}],
        },
    )
    assert load_config(cfg).targets[0].where == {"回答者": "佐藤"}
    _write(cfg, {"targets": [{"name": "q", "source": "${root}/a.xlsx"}]})
    with pytest.raises(ConfigError, match=r"\$\{root\}"):
        load_config(cfg)


def test_include_merges_target_files(tmp_path: Path) -> None:
    (tmp_path / "targets.d").mkdir()
    _write(tmp_path / "targets.d" / "a.yaml", {"targets": [{"name": "A", "source": "${base}/a.md"}]})
    _write(tmp_path / "targets.d" / "b.yaml", {"targets": [{"name": "B", "source": "b.md"}]})
    cfg = _write(
        tmp_path / "t.yaml",
        {"vars": {"base": "C:/x"}, "include": ["targets.d/*.yaml"], "targets": [{"name": "main", "source": "m.md"}]},
    )
    c = load_config(cfg)
    assert [t.name for t in c.targets] == ["main", "A", "B"]
    assert c.targets[1].source == "C:/x/a.md"


def test_duplicate_names_and_bad_values(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "t.yaml", {"targets": [{"name": "q", "source": "a"}, {"name": "q", "source": "b"}]})
    with pytest.raises(ConfigError, match="2回以上"):
        load_config(cfg)
    _write(cfg, {"targets": [{"name": "q", "source": "a", "every": "soon"}]})
    with pytest.raises(ConfigError, match="30m"):
        load_config(cfg)
    _write(cfg, {"targets": [{"name": "q", "source": "a", "report": ["new"]}]})
    with pytest.raises(ConfigError, match="使える値"):
        load_config(cfg)


def test_kind_is_inferred_from_the_source(tmp_path: Path) -> None:
    assert Target(name="a", source=str(tmp_path)).kind == "folder"
    assert Target(name="a", source="x/*.docx").kind == "folder"
    assert Target(name="a", source="x/a.xlsx").kind == "file"
    assert Target(name="a", source="https://example.com/news").kind == "web"
    assert Target(name="a", source="https://r.example.com/issues.json?limit=100").kind == "json"
    assert Target(name="a", source="https://x.atlassian.net/wiki/spaces/K/pages/123/T").kind == "confluence"
    assert Target(name="a", source="https://h/confluence/pages/viewpage.action?pageId=9").kind == "confluence"
    assert Target(name="a", source="https://c.sharepoint.com/sites/s/Shared%20Documents/q.xlsx").kind == "office"
    with pytest.raises(ValueError, match="http"):
        Target(name="a", source="C:/a.html", fetch="web")


def test_interval_and_template_is_valid(tmp_path: Path) -> None:
    import datetime as dt

    from watchpoint.config import TEMPLATE

    assert Target(name="a", source="a", every="2h").interval == dt.timedelta(hours=2)
    cfg = tmp_path / "t.yaml"
    cfg.write_text(TEMPLATE, encoding="utf-8")
    c = load_config(cfg)
    assert c.targets[0].where == {"回答者": "佐藤"}
    assert c.targets[0].source == r"\\server\share\project\質問表.xlsx"
