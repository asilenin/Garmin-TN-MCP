"""Интеграционный тест 8a+8a.1: add_lactate (три формы входа + якорная конвертация),
add_note/delete, recompute_user_marks, мёрж в compact/full, + замок профиль-
нейтральности возврата (I2/Q4 7.5). Штатный интеграционный тест репо: самодостаточен
(temp-БД, синтетика), путь к модулям от __file__ — бежит из любого cwd и в CI."""
import os, tempfile, sys, json
import numpy as np

tmp = tempfile.mkdtemp()
os.environ["GARMIN_TN_HOME"] = tmp
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "garmin_raw"))  # якорь — __file__, не cwd

import profiles, tools, sync
from store import Store

SLUG = "testp"
prof = profiles.resolve(SLUG); prof.ensure_dirs()
AV = "enrich-0.6.0"
base = 1_700_000_000_000

def md(k, i): return {"key": k, "metricsIndex": i}
def make_stream(ts0=base, n=300):
    ts = (ts0 + np.arange(n) * 1000.0)
    sp = np.full(n, 3.0); hr = np.linspace(140.0, 175.0, n)
    return {"metricDescriptors": [md("directTimestamp",0), md("directSpeed",1), md("directHeartRate",2)],
            "activityDetailMetrics": [{"metrics": [ts[k], sp[k], hr[k]]} for k in range(n)]}
LAPS2 = {"lapDTOs": [{"elapsedDuration":150.0}, {"elapsedDuration":150.0}]}

with Store(prof.db_path) as st:
    assert st.schema_version == 5
    for aid in (111, 222):
        st.conn.execute("INSERT INTO activities(activity_id,date,sport) VALUES(?,?,?)",
                        (aid, "2026-06-27", "running"))
        st.put_raw(aid, "streams", make_stream())
    st.put_raw(111, "laps", LAPS2)
    # 333: НЕТ streams, но есть summary_json с beginTimestamp (якорь уровня 2)
    st.conn.execute("INSERT INTO activities(activity_id,date,sport,start_time,summary_json) VALUES(?,?,?,?,?)",
                    (333, "2026-06-27", "running", "2026-06-27 05:00:00",
                     json.dumps({"beginTimestamp": base})))
    # 444: ни streams, ни summary/start_time → якоря нет
    st.conn.execute("INSERT INTO activities(activity_id,date,sport) VALUES(?,?,?)",
                    (444, "2026-06-27", "running"))
    st.meta_set("algo_version", AV)
    st.conn.commit()

# --- 8a.1: якорная конвертация elapsed→wall-clock ---
# ts[0]=base; at_elapsed 2190с → at_time = base + 2190000 (общий ноль с argmin)
r = tools.add_lactate(SLUG, 111, 5.5, at_elapsed_s=2190)
assert r["at_time"] == base + 2190000, r   # ключевая арифметика конвертации
print("A elapsed→wall-clock (ts0 anchor): at_time", r["at_time"], "==", base+2190000, "✓")
# при ts0=base круг1=[base..base+150k], круг2=[..+300k]; 2190с за пределами 300с-потока
# → правый край, вне допуска → pending_resolve (at_time записан, привязки нет)
assert r["status"] == "pending_resolve", r
# at_ms (сырой wall-clock) внутри потока → resolved
r = tools.add_lactate(SLUG, 111, 6.0, at_ms=base+150_000)
assert r["status"] == "resolved" and abs(r["hr_at"]-157.5) < 1, r
print("B at_ms immediate resolve:", r["status"], "hr", r["hr_at"])

# --- якорь уровня 2: streams НЕТ, beginTimestamp ---
r = tools.add_lactate(SLUG, 333, 5.0, at_elapsed_s=100)
assert r["at_time"] == base + 100_000, r        # beginTimestamp-якорь
assert r["status"] == "pending_resolve", r      # streams нет → резолва нет, at_time записан
print("C level-2 beginTimestamp anchor: at_time", r["at_time"], "status", r["status"], "✓")
# отложенный путь: streams приходят с ts0 = beginTimestamp+1500 (рассинхрон нулей на 1.5с)
with Store(prof.db_path) as st:
    st.put_raw(333, "streams", make_stream(ts0=base+1500)); st.conn.commit()
    # диагностика сверки: beginTimestamp vs ts[0] расходятся на 1500мс
    ts0 = tools._stream_first_ts(st.get_raw(333, "streams"))
    bt = json.loads(st.conn.execute("SELECT summary_json FROM activities WHERE activity_id=333").fetchone()[0])["beginTimestamp"]
    assert ts0 - bt == 1500, (ts0, bt)
    print(f"   диагностика: ts0-beginTimestamp = {ts0-bt}мс (сверка ловит рассинхрон нулей)")
um = sync.recompute_user_marks(SLUG)
# метка 333 (at_time=base+100000) резолвится по ts0=base+1500: ближайшая секунда argmin
mk333 = [m for m in tools.get_activity_full(SLUG,333)["user_marks"] if m["kind"]=="lactate"][0]
print("   после дозакачки status:", mk333["status"], "(argmin по ts0, не beginTimestamp)")

# --- якоря нет → error ---
assert "error" in tools.add_lactate(SLUG, 444, 5.0, at_elapsed_s=100), "нет якоря → error"
print("D no anchor → error ✓")

# --- 8a базовые ветки (сохранены) ---
assert tools.add_lactate(SLUG, 111, 3.5, user_ref="lap1")["status"] == "resolved"
assert "error" in tools.add_lactate(SLUG, 111, 4.0, user_ref="lap9")   # invalid на входе
assert "error" in tools.add_lactate(SLUG, 111, 4.0)                    # нечего привязывать
assert "error" in tools.add_lactate(SLUG, 999, 4.0, at_ms=base)       # not found
assert "error" in tools.add_lactate(SLUG, 111, 4.0, user_ref="круг4")  # malformed
r = tools.add_lactate(SLUG, 222, 5.0, user_ref="lap4")                # laps нет → deferred
assert r["status"] == "pending_validation", r
print("E input branches (user_ref/invalid/deferred/errors) OK")

# add_note + id-чек
assert "mark_id" in tools.add_note(SLUG, 111, "интервалка")
assert "error" in tools.add_note(SLUG, 999, "осиротеть не должна")

# мёрж compact/full
c = tools.get_activity_compact(SLUG, 111)
assert "user_marks" in c and any(m["kind"]=="note" for m in c["user_marks"])
assert "user_marks" in tools.get_activity_full(SLUG, 111)

# recompute deferred→invalid + revival
with Store(prof.db_path) as st:
    st.put_raw(222, "laps", {"lapDTOs":[{"elapsedDuration":100.0}]*3}); st.conn.commit()
sync.recompute_user_marks(SLUG)
mk = [m for m in tools.get_activity_compact(SLUG,222)["user_marks"] if m["kind"]=="lactate"][0]
assert mk["status"]=="invalid" and mk["lap_count"]==3 and "hr_at" not in mk, mk
with Store(prof.db_path) as st:
    st.put_raw(222, "laps", {"lapDTOs":[{"elapsedDuration":100.0}]*5}); st.conn.commit()
sync.recompute_user_marks(SLUG)
mk = [m for m in tools.get_activity_compact(SLUG,222)["user_marks"] if m["kind"]=="lactate"][0]
assert mk["status"] in ("resolved","pending_resolve"), mk
print("F recompute deferred→invalid→revival OK")

# delete
first = c["user_marks"][0]["mark_id"]
assert tools.delete_lactate(SLUG, first)["deleted"] is True
print("G delete OK")

# --- ЗАМОК профиль-нейтральности возврата (I2 / QA 7.5 Q4) ---
# slug виден функции, невидим модели. Модель видит ВОЗВРАТ семи функций этапа 7 →
# в нём не должно быть ни имён-ключей профиля, ни значений (путей/slug) этого профиля.
# Страж при функциях (бежит при каждом изменении tools.py), не разовая приёмка обёртки.
_KEY_BLACKLIST = {"slug", "db_path", "profile", "tokens_dir", "base", "state_path"}

def assert_profile_neutral(result, resolved):
    """Два инварианта, РАЗНЫЙ способ для путей и slug (Q4):
      1. ни один КЛЮЧ (рекурсивно) не из чёрного списка имён;
      2. ПУТИ (db_path/tokens_dir/base) — подстрокой в любом строковом листе
         (путь в тексте = всегда утечка, в данных активности легитимно не появится);
      3. SLUG — ТОЧНЫМ равенством строкового листа (не подстрокой: заметка —
         свободный текст, штатно содержит имя владельца, напр. 'бежал с Антоном').
    """
    paths = [str(resolved.db_path), str(resolved.tokens_dir), str(resolved.base)]
    slug_val = resolved.slug

    def walk(node, path="root"):
        if isinstance(node, dict):
            for k, v in node.items():
                assert k not in _KEY_BLACKLIST, f"утечка имени ключа {k!r} в {path}"
                walk(v, f"{path}.{k}")
        elif isinstance(node, (list, tuple)):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, str):
            for p in paths:                       # пути — подстрокой
                assert p not in node, f"утечка пути {p!r} в строке {path}"
            assert node != slug_val, f"утечка slug {slug_val!r} значением в {path}"

    walk(result)

# прогон по ВСЕМ семи функциям этапа 7 под известным профилем (SLUG='testp').
# Спец-кейсы, специально провоцирующие протечку:
#  - заметка, СОДЕРЖАЩАЯ slug как подстроку ('пробежка testp стайл') → не должна падать
#    (slug проверяется равенством, не подстрокой);
#  - ошибки (not found / malformed) — тоже через замок.
resolved = profiles.resolve(SLUG)
note_id = tools.add_note(SLUG, 111, f"пробежка {SLUG} стайл, бежал с owner")["mark_id"]
_returns = [
    tools.query_index(SLUG, limit=5),
    tools.get_activity_compact(SLUG, 111),
    tools.get_activity_compact(SLUG, 999),               # error: not found
    tools.get_activity_full(SLUG, 111),
    tools.get_period_aggregates(SLUG),
    tools.add_lactate(SLUG, 111, 5.0, at_ms=base + 60_000),
    tools.add_lactate(SLUG, 111, 5.0, user_ref="кривой"),  # error: malformed
    tools.add_note(SLUG, 111, "заметка"),
    tools.delete_lactate(SLUG, note_id),
]
for r_ in _returns:
    assert_profile_neutral(r_, resolved)
# контроль-позитив: замок ДЕЙСТВИТЕЛЬНО ловит утечку (иначе он мёртв и мы не знаем)
_leaked = False
try:
    assert_profile_neutral({"profile": SLUG}, resolved)          # имя ключа
except AssertionError:
    try:
        assert_profile_neutral({"x": str(resolved.db_path)}, resolved)  # путь подстрокой
    except AssertionError:
        try:
            assert_profile_neutral({"x": SLUG}, resolved)        # slug значением
        except AssertionError:
            _leaked = True
assert _leaked, "ЗАМОК НЕ ЛОВИТ утечку — он мёртв"
print("H profile-neutrality lock OK (7 функций + контроль-позитив)")

print("8A+8A.1 INTEGRATION TEST PASSED")
