"""test_ref_lint.py — страж REF-BY-NAME (INVARIANTS.md, DEV_RULES §7).

Запрещает в коде: (1) устаревшие имена доков (слетают при переименовании);
(2) голые Q-ссылки Q<N> без квалификации (Q-номера локальны этапу → коллизия).
Разрешено: ключи кварталов \\d{4}-Q\\d ('2026-Q2'), формат 'YYYY-Qn', открытый
ресёрч-референс 'Q-8.1' (этап 8) — все они не матчатся паттерном ниже по построению
(перед Q слово-символ/дефис, либо за Q нет цифры)."""
import re, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SELF = pathlib.Path(__file__).name

# Голый Q<цифра>: Q не в хвосте слова и не после дефиса (кварталы), сразу цифра (Q-8.1 не в счёт).
BARE_Q = re.compile(r"(?<![\w-])Q\d+\b")
# Устаревшие имена доков — собраны конкатенацией, чтобы страж не ловил сам себя.
STALE_DOCS = ["TN_Garmin" + "_MCP", "metod_chteniya" + "_trenirovok"]

def _files():
    for d in ("garmin_raw", "tests"):
        for p in (ROOT / d).glob("*.py"):
            if p.name != SELF:
                yield p

def test_no_bare_q_refs():
    bad = []
    for p in _files():
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            for m in BARE_Q.finditer(line):
                bad.append(f"{p.relative_to(ROOT)}:{i}: голый {m.group(0)} — квалифицируй именем инварианта (INVARIANTS.md)")
    assert not bad, "Голые Q-ссылки (REF-BY-NAME):\n" + "\n".join(bad)

def test_no_stale_doc_names():
    bad = []
    for p in _files():
        txt = p.read_text(encoding="utf-8")
        for s in STALE_DOCS:
            if s in txt:
                bad.append(f"{p.relative_to(ROOT)}: устаревшее имя дока {s!r} → TN_Run_MCP_*")
    assert not bad, "Устаревшие имена доков:\n" + "\n".join(bad)

if __name__ == "__main__":
    test_no_bare_q_refs(); test_no_stale_doc_names()
    print("test_ref_lint ✓ — ноль голых Q, ноль устаревших имён доков")
