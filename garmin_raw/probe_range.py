import time
from garminconnect import Garmin

c = Garmin()
c.login("~/.garminconnect")

# 1) сколько активностей реально вернётся за «всю историю»
t = time.time()
acts = c.get_activities_by_date("2010-01-01", "2026-12-31", "running")
print(f"get_activities_by_date: {len(acts)} активностей за {time.time()-t:.1f} c")

if acts:
    # порядок? первая/последняя в выдаче — это начало или конец архива?
    def d(a): return a.get("startTimeLocal") or a.get("startTimeGMT")
    print("первая в списке:", d(acts[0]))
    print("последняя в списке:", d(acts[-1]))
    print("min дата:", min(d(a) for a in acts))
    print("max дата:", max(d(a) for a in acts))

# 2) есть ли дешёвый эндпоинт «сколько всего / с какой даты» без выкачивания списка
for name in ("get_user_summary", "get_userprofile", "get_stats",
             "get_activities", "get_progress_summary_between_dates"):
    fn = getattr(c, name, None)
    print(f"{name}: {'есть' if callable(fn) else 'нет'}")
