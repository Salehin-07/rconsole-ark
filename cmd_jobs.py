"""Background-job commands: jobs / jobout / jobtail / fg / kill %n."""
import shlex

import commands
import jobs


@commands.command("jobs")
def cmd_jobs(ctx, args):
    """List background jobs started with '&'."""
    sid = ctx["sid"]
    listing = jobs.list_jobs(sid)
    if not listing:
        return "No background jobs running. Start one with:  sleep 30 &"
    out = ["Job   PID    STATE      COMMAND", "-" * 50]
    for jid, job in listing:
        state_ = "running" if job.alive else "done"
        pid = str(job.pid) if job.pid else "-"
        out.append(f"  {jid:<3}  {pid:<6} {state_:<10} {job.cmd[:40]}")
    out.append("")
    out.append("Inspect output:  jobout %n   |   Live:  jobtail %n / fg %n   |   Kill:  kill %n")
    return "\n".join(out)


@commands.command("jobout")
def cmd_jobout(ctx, args):
    """Show the last lines of a background job's output: jobout %n [lines]."""
    if not args:
        return "Usage: jobout %n [lines]"
    jid = jobs.parse_job_ref(args[0])
    if jid is None:
        return f"jobout: invalid job: {args[0]}"
    job = jobs.get_job(ctx["sid"], jid)
    if not job:
        return f"jobout: no such job: {jid}"
    n = 50
    if len(args) > 1:
        try:
            n = int(args[1])
        except ValueError:
            return "jobout: lines must be an integer"
    head = f"--- job {jid} ({'running' if job.alive else 'done'}) ---"
    return head + "\n" + job.tail(n)


@commands.command("jobtail")
def cmd_jobtail(ctx, args):
    """Live-tail a background job's output: jobtail %n (press any key to stop)."""
    if not args:
        return "Usage: jobtail %n   (live tail; press any key to stop)"
    jid = jobs.parse_job_ref(args[0])
    if jid is None:
        return f"jobtail: invalid job: {args[0]}"
    job = jobs.get_job(ctx["sid"], jid)
    if not job:
        return f"jobtail: no such job: {jid}"
    return {"jobtail": jid}


@commands.command("fg")
def cmd_fg(ctx, args):
    """Bring a background job's output to the foreground: fg %n.

    Streams the job's output live and stops automatically when the job ends.
    """
    if not args:
        return "Usage: fg %n   (foreground a job; stops when it ends)"
    jid = jobs.parse_job_ref(args[0])
    if jid is None:
        return f"fg: invalid job: {args[0]}"
    job = jobs.get_job(ctx["sid"], jid)
    if not job:
        return f"fg: no such job: {jid}"
    return {"jobtail": jid, "fg": True}


@commands.command("kill")
def cmd_kill(ctx, args):
    """Kill processes.

    Usage:
      kill %n          -> kill background job n (see: jobs)
      kill <pid> ...   -> delegate to the real host kill
      kill <signal> %n -> send a signal name to job n (e.g. kill SIGKILL %1)
    """
    import interp as _interp  # lazy: interp imports this registry at load time
    if not args:
        return "Usage: kill %n | kill <pid>..."
    # Job reference '%n' anywhere -> kill that job.
    for a in args:
        if a.startswith("%"):
            jid = jobs.parse_job_ref(a)
            if jid is None:
                return f"kill: invalid job: {a}"
            if jobs.kill_job(ctx["sid"], jid):
                return f"Background job {jid} terminated."
            return f"kill: no such job: {jid}"
    # Otherwise delegate to the real host kill (e.g. kill -9 1234).
    return _interp._passthrough(ctx, "kill " + shlex.join(args))
