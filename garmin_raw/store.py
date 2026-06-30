"""store.py — слой SQLite: источник правды одного профиля (§3 ТЗ).

Одна БД на профиль. Версия СХЕМЫ — через PRAGMA user_version + раннер миграций
(структура таблиц меняется независимо от версии ФОРМУЛ — §7 ТЗ). Прикладное
состояние (algo_version, диапазон данных, политика) — в key-value таблице meta.

Главный инвариант (§1 ТЗ): БД — источник правды, не ускоритель. Сырьё завершённой
тренировки неизменно → хранится навечно, без TTL; обогащение пересчитывается из
локального сырья, не из сети.

Этот файл — этап 1 (§14): схема + миграции + CRUD каталога/meta/raw. CRUD для
enriched/aggregates добавляется на этапах 4–5; таблицы создаются уже сейчас.
"""
from __future__ import annotations

import json
import sqlite3
import time
import zlib
from pathlib import Path
from typing import Any, Iterable, Optional

# Версия СХЕМЫ БД (структура таблиц). НЕ путать с ALGO_VERSION (версия формул).
SCHEMA_VERSION = 2


# --------------------------------------------------------------------------- #
# Миграции. Каждая — (версия, SQL). Применяются по возрастанию для всех версий
# больше текущего PRAGMA user_version. Никогда не переписывать прошлые миграции —
# только добавлять новые (иначе чужие БД не мигрируют корректно).
# --------------------------------------------------------------------------- #
_MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        -- §3.1 каталог: дёшево, для ВСЕХ тренировок, никогда не выкидывается.
        -- Колонки делятся по роли (см. ТЗ §3.1, финальная схема):
        --  НАДЁЖНЫЕ, фильтруемые: max_hr, distance_m, duration_s, moving_time_s,
        --                         avg_cadence, флаги достоверности
        --  ОБМАНЧИВЫЕ (_raw): avg_hr_raw, avg_speed_raw — среднее разрушено
        --                     диапазоном, НЕ фильтруемые, суффикс = «не верь никогда»
        --  ФЛАГ НАЛИЧИЯ ДАТЧИКА: avg_gct, avg_vert_ratio — не аналитика
        --  ЧУЖАЯ ПРОИЗВОДНАЯ: garmin_training_load_derived — не фильтр (§1 запрет)
        --  ИСТОЧНИК ИСТИНЫ: summary_json — всё, что не разложено в колонки
        CREATE TABLE IF NOT EXISTS activities (
            activity_id     INTEGER PRIMARY KEY,
            date            TEXT,        -- YYYY-MM-DD (с годом)
            start_time      TEXT,        -- ISO/UTC
            sport           TEXT,
            duration_s      REAL,        -- надёжное, фильтруемое
            distance_m      REAL,        -- надёжное, фильтруемое
            moving_time_s   REAL,        -- NULL до обогащения; надёжное, фильтруемое
            lap_count       INTEGER,
            max_hr          INTEGER,     -- надёжное, фильтруемое
            avg_cadence     REAL,        -- фильтруемое БЕЗ _raw (узкий диапазон; край strides снимается median_crossings/lap_count)
            avg_hr_raw      REAL,        -- НЕ фильтруемое: среднее разрушено диапазоном на интервалах (§5.1)
            avg_speed_raw   REAL,        -- НЕ фильтруемое: то же
            avg_gct         REAL,        -- флаг наличия датчика, не аналитика (привязка к темпу — на обогащении)
            avg_vert_ratio  REAL,        -- флаг наличия датчика
            garmin_training_load_derived REAL,  -- чужая производная Garmin; НЕ фильтр (§1)
            has_biomech_sensor INTEGER,  -- флаг достоверности §3.2; NULL до уточнения
            gps_validated      INTEGER,  -- флаг достоверности §3.2; NULL до уточнения
            device_model    TEXT,        -- заполняется классификацией позже
            hr_source       TEXT,        -- chest/optical/unknown
            gps_type        TEXT,        -- outdoor/treadmill/track/none
            biomech_source  TEXT,        -- run-pod/foot-pod/watch-only
            summary_json    TEXT         -- сырая сводка (strip_pii), источник истины
        );
        CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date);
        CREATE INDEX IF NOT EXISTS idx_activities_distance ON activities(distance_m);
        CREATE INDEX IF NOT EXISTS idx_activities_maxhr ON activities(max_hr);

        -- §3.2 сырьё: навечно, без TTL. Прошлое неизменно.
        CREATE TABLE IF NOT EXISTS activity_raw (
            activity_id  INTEGER NOT NULL,
            kind         TEXT NOT NULL,      -- laps/streams/comment
            payload      BLOB NOT NULL,      -- zlib(JSON utf-8)
            fetched_at   TEXT NOT NULL,
            PRIMARY KEY (activity_id, kind)
        );

        -- §3.3 индекс интересности: дёшево, для ВСЕХ; пере-выбор A-множества без перекачки
        CREATE TABLE IF NOT EXISTS interest_index (
            activity_id       INTEGER PRIMARY KEY,
            variability       REAL,
            median_crossings  INTEGER,
            hr_above_easy_s   REAL,
            interest_score    REAL,
            algo_version      TEXT
        );

        -- §3.4 обогащение per-activity: версионировано (ключ включает algo_version)
        CREATE TABLE IF NOT EXISTS activity_enriched (
            activity_id     INTEGER NOT NULL,
            algo_version    TEXT NOT NULL,
            hr_histogram    TEXT,
            pace_histogram  TEXT,
            clusters        TEXT,
            pace_variance   REAL,
            hr_variance     REAL,
            biomech_by_pace TEXT,
            lactate_marks   TEXT,
            elevation       TEXT,
            confidence      TEXT,
            computed_at     TEXT,
            PRIMARY KEY (activity_id, algo_version)
        );

        -- §3.5 кросс-активностные агрегаты: считает КОННЕКТОР, не LLM; версионировано
        CREATE TABLE IF NOT EXISTS period_aggregates (
            period_key             TEXT NOT NULL,   -- напр. 2026-Q2
            algo_version           TEXT NOT NULL,
            pace_at_fixed_hr       TEXT,
            gct_at_fixed_pace      TEXT,
            decoupling             TEXT,
            hr_recovery            TEXT,
            intensity_distribution TEXT,
            volume_7d              TEXT,
            volume_28d             TEXT,
            max_hr_accumulated     INTEGER,
            computed_at            TEXT,
            PRIMARY KEY (period_key, algo_version)
        );

        -- §3.6 прикладное состояние БД (key-value)
        CREATE TABLE IF NOT EXISTS meta (
            key    TEXT PRIMARY KEY,
            value  TEXT
        );
        """,
    ),
    (
        2,
        """
        -- ─────────────────────────────────────────────────────────────────────
        -- Миграция v2 (этап 5). Две независимые правки, обе безопасны для сырья:
        --   (1) period_aggregates: первая редакция (v1) была СТАРОЙ схемой с
        --       зашитыми якорями (pace_at_fixed_hr, gct_at_fixed_pace) и именами
        --       (intensity_distribution) — ТЗ §13 запрещает их коннектору.
        --       Таблица ПУСТА (этап 5 не делался, CRUD записи не было) → DROP+CREATE
        --       без переноса данных безопасен. Новая схема — §3.5 ТЗ:
        --       якорь-нейтральные сетки by_source + provenance, без имён/зон.
        --   (2) activity_enriched: ALTER ADD 2 колонки под бакетные выжимки
        --       enrich-0.3.0. ALTER ADD COLUMN в SQLite — метаданные-операция, НЕ
        --       переписывает таблицу, НЕ трогает activity_raw. Старые 0.2.2 строки
        --       получают NULL в новых полях и остаются ВАЛИДНЫМИ для чтения; новые
        --       0.3.0 строки пишутся рядом под своим ключом (recompute, resumable).
        -- Ни один оператор не касается activity_raw/activities/interest_index/meta.
        -- ─────────────────────────────────────────────────────────────────────

        DROP TABLE IF EXISTS period_aggregates;
        CREATE TABLE period_aggregates (
            period_key          TEXT NOT NULL,   -- напр. 2026-Q2 (квартальные срезы, МЕТОД §5.4)
            algo_version        TEXT NOT NULL,
            -- якорь-нейтральные, без имён (§3.5):
            volume_7d           TEXT,    -- скользящее окно, км (якоря нет)
            volume_28d          TEXT,
            max_hr_accumulated  INTEGER, -- ~97-й перцентиль распределения per-activity max, НЕ max() (§2.4)
            decoupling          TEXT,    -- механический ratio (2-я пол./1-я), без имени «база держит» (LLM)
            hr_recovery         TEXT,    -- падение HR после кругов быстрее медианы темпа, без имени (LLM)
            pace_by_hr_grid     TEXT,    -- темп по сетке HR, by_source {chest:{...},optical:{...}} (§3.5.1)
            gct_by_pace_grid    TEXT,    -- GCT/vert-ratio по сетке темпа, одной строкой (биомеханика железо-незав.)
            provenance          TEXT,    -- ФАКТ происхождения: hr_sources/device_models с долями и n; без суждения
            computed_at         TEXT,
            PRIMARY KEY (period_key, algo_version)
        );

        -- бакетные выжимки enrich-0.3.0 (per-row версия в PK уже различает 0.2.2/0.3.0)
        ALTER TABLE activity_enriched ADD COLUMN biomech_by_pace_bucket TEXT;
        ALTER TABLE activity_enriched ADD COLUMN pace_by_hr_bucket TEXT;
        """,
    ),
]


def _compress(obj: Any) -> bytes:
    return zlib.compress(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def _decompress(blob: bytes) -> Any:
    return json.loads(zlib.decompress(blob).decode("utf-8"))


class Store:
    """Соединение с БД профиля + операции. Контекстный менеджер.

    Использование:
        with Store(profile.db_path) as st:
            st.upsert_activities(rows)
            start, end = st.activity_date_range()
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")       # конкурентное чтение при sync
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self._migrate()

    # ------------------------------------------------------------------ #
    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ #
    # Миграции
    # ------------------------------------------------------------------ #
    def _migrate(self) -> None:
        cur = self.conn.execute("PRAGMA user_version;")
        version = cur.fetchone()[0]
        for target, sql in _MIGRATIONS:
            if target > version:
                self.conn.executescript(sql)
                self.conn.execute(f"PRAGMA user_version={target};")
                self.conn.commit()
                version = target

    @property
    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version;").fetchone()[0]

    # ------------------------------------------------------------------ #
    # meta (key-value)
    # ------------------------------------------------------------------ #
    def meta_get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # Каталог activities
    # ------------------------------------------------------------------ #
    _ACT_COLS = (
        "activity_id", "date", "start_time", "sport", "duration_s", "distance_m",
        "moving_time_s", "lap_count", "max_hr", "avg_cadence", "avg_hr_raw",
        "avg_speed_raw", "avg_gct", "avg_vert_ratio", "garmin_training_load_derived",
        "has_biomech_sensor", "gps_validated", "device_model", "hr_source",
        "gps_type", "biomech_source", "summary_json",
    )

    def upsert_activities(self, rows: Iterable[dict]) -> int:
        """Вставка/обновление каталожных строк. Не затирает поля, которых нет в row,
        кроме явно переданных. Возвращает число обработанных строк."""
        cols = self._ACT_COLS
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "activity_id")
        sql = (
            f"INSERT INTO activities ({','.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(activity_id) DO UPDATE SET {updates}"
        )
        data = [tuple(r.get(c) for c in cols) for r in rows]
        self.conn.executemany(sql, data)
        self.conn.commit()
        return len(data)

    def activity_date_range(self) -> tuple[Optional[str], Optional[str]]:
        """min/max date по каталогу — это и есть garmin_range (§12.1, побочный продукт)."""
        row = self.conn.execute(
            "SELECT MIN(date) AS lo, MAX(date) AS hi FROM activities"
        ).fetchone()
        return (row["lo"], row["hi"])

    def activity_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]

    def has_raw(self, activity_id: int, kind: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM activity_raw WHERE activity_id=? AND kind=?",
            (activity_id, kind),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------ #
    # Обогащение activity_enriched (версионировано по algo_version)
    # ------------------------------------------------------------------ #
    _ENR_COLS = (
        "activity_id", "algo_version", "hr_histogram", "pace_histogram", "clusters",
        "pace_variance", "hr_variance", "biomech_by_pace", "lactate_marks",
        "elevation", "confidence", "computed_at",
        "biomech_by_pace_bucket", "pace_by_hr_bucket",
    )

    def has_enriched(self, activity_id: int, algo_version: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM activity_enriched WHERE activity_id=? AND algo_version=?",
            (activity_id, algo_version),
        ).fetchone()
        return row is not None

    def put_enriched(self, activity_id: int, enriched: dict) -> None:
        """Пишет результат enrich_activity. JSON-поля сериализуются. Идемпотентно
        по (activity_id, algo_version) — пересчёт перезаписывает.

        Дополнительно синхронизирует moving_time_s и median_crossings в каталог
        (там они были NULL до обогащения)."""
        av = enriched.get("algo_version")
        row = {
            "activity_id": activity_id,
            "algo_version": av,
            "hr_histogram": json.dumps(enriched.get("hr_histogram"), ensure_ascii=False),
            "pace_histogram": json.dumps(enriched.get("pace_histogram"), ensure_ascii=False),
            "clusters": json.dumps(enriched.get("clusters"), ensure_ascii=False),
            "pace_variance": enriched.get("pace_variance"),
            "hr_variance": enriched.get("hr_variance"),
            "biomech_by_pace": json.dumps(enriched.get("biomech_by_pace"), ensure_ascii=False),
            "lactate_marks": json.dumps(enriched.get("lactate_marks"), ensure_ascii=False),
            "elevation": json.dumps(enriched.get("elevation"), ensure_ascii=False),
            "confidence": json.dumps(enriched.get("confidence"), ensure_ascii=False),
            "computed_at": _iso_now(),
            "biomech_by_pace_bucket": json.dumps(enriched.get("biomech_by_pace_bucket"), ensure_ascii=False),
            "pace_by_hr_bucket": json.dumps(enriched.get("pace_by_hr_bucket"), ensure_ascii=False),
        }
        cols = self._ENR_COLS
        ph = ",".join("?" for _ in cols)
        upd = ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("activity_id", "algo_version"))
        self.conn.execute(
            f"INSERT INTO activity_enriched ({','.join(cols)}) VALUES ({ph}) "
            f"ON CONFLICT(activity_id,algo_version) DO UPDATE SET {upd}",
            tuple(row[c] for c in cols),
        )
        # каталог: проставляем посчитанные обогащением поля
        mt = enriched.get("moving_time_s")
        mc = enriched.get("median_crossings")
        mx = enriched.get("max_hr")
        self.conn.execute(
            "UPDATE activities SET moving_time_s=COALESCE(?,moving_time_s), "
            "max_hr=COALESCE(?,max_hr) WHERE activity_id=?",
            (mt, mx, activity_id),
        )
        # median_crossings храним в interest_index (дешёвый сигнал для отбора/фильтра)
        self.conn.execute(
            "INSERT INTO interest_index(activity_id,median_crossings,algo_version) "
            "VALUES(?,?,?) ON CONFLICT(activity_id) DO UPDATE SET "
            "median_crossings=excluded.median_crossings, algo_version=excluded.algo_version",
            (activity_id, mc, av),
        )
        self.conn.commit()

    def get_enriched(self, activity_id: int, algo_version: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM activity_enriched WHERE activity_id=? AND algo_version=?",
            (activity_id, algo_version),
        ).fetchone()
        if not row:
            return None
        out = dict(row)
        for k in ("hr_histogram", "pace_histogram", "clusters", "biomech_by_pace",
                  "lactate_marks", "elevation", "confidence",
                  "biomech_by_pace_bucket", "pace_by_hr_bucket"):
            if out.get(k):
                out[k] = json.loads(out[k])
        return out

    def activity_ids(self, *, start: Optional[str] = None, end: Optional[str] = None,
                     sport: Optional[str] = None, limit: Optional[int] = None,
                     order_desc: bool = True) -> list[int]:
        """ID каталога по фильтру дат/спорта — для батчей обогащения и query_index."""
        clauses, params = [], []
        if start:
            clauses.append("date >= ?"); params.append(start)
        if end:
            clauses.append("date <= ?"); params.append(end)
        if sport:
            clauses.append("sport = ?"); params.append(sport)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        order = "DESC" if order_desc else "ASC"
        lim = f" LIMIT {int(limit)}" if limit else ""
        sql = f"SELECT activity_id FROM activities{where} ORDER BY date {order}{lim}"
        return [r[0] for r in self.conn.execute(sql, params).fetchall()]

    # ------------------------------------------------------------------ #
    # Сырьё activity_raw (zlib JSON)
    # ------------------------------------------------------------------ #
    def put_raw(self, activity_id: int, kind: str, payload: Any) -> None:
        self.conn.execute(
            "INSERT INTO activity_raw(activity_id,kind,payload,fetched_at) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(activity_id,kind) DO UPDATE SET "
            "payload=excluded.payload, fetched_at=excluded.fetched_at",
            (activity_id, kind, _compress(payload), _iso_now()),
        )
        self.conn.commit()

    def get_raw(self, activity_id: int, kind: str) -> Optional[Any]:
        row = self.conn.execute(
            "SELECT payload FROM activity_raw WHERE activity_id=? AND kind=?",
            (activity_id, kind),
        ).fetchone()
        return _decompress(row["payload"]) if row else None

    # ------------------------------------------------------------------ #
    # Статус (для cache_status / CLI)
    # ------------------------------------------------------------------ #
    def status(self) -> dict:
        lo, hi = self.activity_date_range()
        raw_cnt = self.conn.execute("SELECT COUNT(*) FROM activity_raw").fetchone()[0]
        enr_cnt = self.conn.execute("SELECT COUNT(*) FROM activity_enriched").fetchone()[0]
        return {
            "schema_version": self.schema_version,
            "algo_version": self.meta_get("algo_version"),
            "download_policy": self.meta_get("download_policy"),
            "activities": self.activity_count(),
            "garmin_range": [lo, hi],
            "raw_rows": raw_cnt,
            "enriched_rows": enr_cnt,
            "last_sync": self.meta_get("last_sync"),
            "db_bytes": self.db_path.stat().st_size if self.db_path.exists() else 0,
        }


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --------------------------------------------------------------------------- #
# Маппинг сырой сводки Garmin → строка каталога.
# Дешёвые поля извлекаем; device/source оставляем NULL — их присваивает
# классификация на этапе обогащения (§3.2, не угадываем здесь). summary_json
# всегда сохраняем целиком, чтобы ничего не потерять.
# --------------------------------------------------------------------------- #
def activity_row_from_summary(summary: dict) -> dict:
    def g(*keys):
        for k in keys:
            if k in summary and summary[k] is not None:
                return summary[k]
        return None

    start = g("startTimeGMT", "startTimeLocal")
    date = start.split(" ")[0].split("T")[0] if isinstance(start, str) else None
    sport = None
    at = summary.get("activityType")
    if isinstance(at, dict):
        sport = at.get("typeKey")
    # флаг наличия бегового датчика: есть осмысленные биомех-поля в сводке
    gct = g("avgGroundContactTime")
    vr = g("avgVerticalRatio")
    has_biomech = 1 if (gct is not None or vr is not None) else 0
    return {
        "activity_id": g("activityId"),
        "date": date,
        "start_time": start,
        "sport": sport,
        "duration_s": g("duration", "elapsedDuration"),
        "distance_m": g("distance"),
        "moving_time_s": g("movingDuration"),  # уточняется обогащением (§6)
        "lap_count": g("lapCount"),
        "max_hr": g("maxHR"),
        # надёжный фильтруемый каденс (без _raw, см. ТЗ §3.1/§5.1)
        "avg_cadence": g("averageRunningCadenceInStepsPerMinute"),
        # обманчивые средние — суффикс _raw, не фильтруются
        "avg_hr_raw": g("averageHR"),
        "avg_speed_raw": g("averageSpeed"),
        # флаги наличия датчика, не аналитика
        "avg_gct": gct,
        "avg_vert_ratio": vr,
        # чужая производная Garmin — помечена, не фильтр
        "garmin_training_load_derived": g("activityTrainingLoad"),
        # флаги достоверности §3.2 (gps_validated уточняется позже — пока NULL)
        "has_biomech_sensor": has_biomech,
        "gps_validated": None,
        "device_model": None,
        "hr_source": None,
        "gps_type": None,
        "biomech_source": None,
        "summary_json": json.dumps(summary, ensure_ascii=False),
    }


if __name__ == "__main__":
    # самопроверка: создать временную БД, прогнать миграции, вставить строку
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        st = Store(Path(d) / "t.db")
        assert st.schema_version == SCHEMA_VERSION, st.schema_version
        row = activity_row_from_summary({
            "activityId": 123, "startTimeGMT": "2026-06-27 10:00:15",
            "activityType": {"typeKey": "running"}, "distance": 12170.0,
            "duration": 3600.0, "maxHR": 171,
            "averageHR": 134, "averageSpeed": 3.4,
            "averageRunningCadenceInStepsPerMinute": 178,
            "avgGroundContactTime": 240, "avgVerticalRatio": 7.1,
            "activityTrainingLoad": 210,
        })
        assert row["avg_cadence"] == 178, row["avg_cadence"]
        assert row["avg_hr_raw"] == 134
        assert row["avg_speed_raw"] == 3.4
        assert row["has_biomech_sensor"] == 1, row["has_biomech_sensor"]
        assert row["garmin_training_load_derived"] == 210
        st.upsert_activities([row])
        st.put_raw(123, "laps", {"lapDTOs": [{"x": 1}]})
        assert st.get_raw(123, "laps") == {"lapDTOs": [{"x": 1}]}
        st.meta_set("algo_version", "0.0.0")
        st.meta_set("download_policy", "all")
        import pprint
        pprint.pp(st.status())
        st.close()
        print("self-test OK")
