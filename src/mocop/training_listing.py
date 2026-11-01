"""Refresh new and active jobs without rereading terminal history every minute."""


def collect_listing(monitor, started, terminal, error_type):
    previous = {job["id"]: job for job in monitor.jobs}
    full = (
        not monitor.complete
        or not previous
        or started - monitor.full_list_collected_at >= 900
    )
    recent = {}
    total = 0
    for page in range(1, 51):
        if monitor.stop.is_set():
            return None
        data = monitor.read(f"/pt/api/ai/jobs?page={page}&page_size=100")
        if (
            not isinstance(data.get("jobs"), list)
            or type(data.get("total")) is not int
            or data["total"] < 0
        ):
            raise error_type(502)
        total = data["total"]
        full = full or total < monitor.total
        rows = [monitor.job(row) for row in data["jobs"][:100]]
        known_boundary = any(row["id"] in previous for row in rows)
        recent.update((row["id"], row) for row in rows)
        if len(recent) >= total or len(rows) < 100 or (not full and known_boundary):
            break
    if not full:
        # Long-running jobs can fall outside the recent pages. Revalidate each one.
        for job_id, job in list(previous.items()):
            if job_id in recent or job["status"] in terminal:
                continue
            try:
                current = monitor.job(
                    monitor.read(f"/pt/api/ai/jobs/by-id/{job_id}").get("job")
                )
                if current["id"] != job_id:
                    raise error_type(502)
                recent[job_id] = current
            except error_type as error:
                if error.status not in (403, 404):
                    raise
                previous.pop(job_id, None)
        recent = {**previous, **recent}
    rows = list(recent.values())
    complete = len(rows) == total
    if full and complete:
        monitor.full_list_collected_at = started
    return rows, complete, total
