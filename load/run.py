"""Run DZ 2 on a disposable Docker Compose project in Debian/WSL."""

import argparse
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time


LOAD = Path(__file__).resolve().parent
REPO = LOAD.parent
PROJECT = "rasul_zeynalov_dz2"
K6_IMAGE = os.environ.get("DZ2_K6_IMAGE", "grafana/k6:0.55.0")
PROFILER_IMAGE = os.environ.get("DZ2_PROFILER_IMAGE", "rasul_zeynalov_dz2-api")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ACTIVITY = """
SELECT json_build_object('total', count(*),
    'active', count(*) FILTER (WHERE state = 'active'),
    'lock', count(*) FILTER (WHERE wait_event_type = 'Lock'))
FROM pg_stat_activity
WHERE datname = current_database() AND backend_type = 'client backend'
  AND pid <> pg_backend_pid()
"""


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def run(*args, **kwargs):
    return subprocess.run(args, check=True, cwd=REPO, text=True, **kwargs)


def docker(*args):
    return run("docker", *args, capture_output=True).stdout.strip()


def compose(*args, workers=1):
    return run(
        "docker", "compose", "-p", PROJECT, "-f", "compose.yaml",
        "-f", "load/compose.load.yaml", *args,
        env={**os.environ, "DZ2_WORKERS": str(workers)}, capture_output=True,
    ).stdout.strip()


def wait_for_http(api):
    command = (
        "import urllib.request; "
        "urllib.request.urlopen('http://127.0.0.1:8000/openapi.json', timeout=2).close()"
    )
    for attempt in range(25):
        try:
            docker("exec", api, "python", "-c", command)
            return
        except subprocess.CalledProcessError:
            if attempt == 24:
                raise
            time.sleep(1)


def idle_sample(api, db):
    data = docker("stats", "--no-stream", "--format", "{{json .}}", api, db)
    return [float(json.loads(line)["CPUPerc"].rstrip("%")) for line in data.splitlines()]


def wait_for_idle(api, db):
    consecutive = 0
    for _ in range(30):
        cpu = idle_sample(api, db)
        consecutive = consecutive + 1 if max(cpu) <= 5 else 0
        if consecutive >= 2:
            return cpu
    raise RuntimeError("CPU не вернулся к простою за 30 замеров")


def pg_activity(stop, db, path):
    with path.open("w", encoding="utf-8") as output:
        while not stop.is_set():
            started = time.monotonic()
            try:
                result = docker(
                    "exec", db, "psql", "-U", "rasul_zeynalov", "-d", "rasul_zeynalov",
                    "-X", "-tAc", ACTIVITY,
                )
                row = json.loads(result)
            except (subprocess.CalledProcessError, ValueError) as error:
                row = {"error": str(error)}
            print(json.dumps({"utc": utc_now(), **row}), file=output, flush=True)
            stop.wait(max(0.0, 1.5 - (time.monotonic() - started)))


def docker_stats(process, path):
    with path.open("w", encoding="utf-8") as output:
        for line in process.stdout:
            try:
                row = json.loads(ANSI.sub("", line).strip())
            except json.JSONDecodeError:
                continue
            print(json.dumps({"utc": utc_now(), **row}), file=output, flush=True)


def read_step(path, rate, seconds):
    metrics = json.loads(path.read_text(encoding="utf-8"))["metrics"]
    latency = metrics["dz2_duration"]["values"]
    errors = metrics["dz2_errors"]["values"]
    dropped = metrics.get("dropped_iterations", {"values": {"count": 0}})["values"]
    completed = metrics["iterations"]["values"]["count"]
    result = {
        "rate": rate, "seconds": seconds, "median_ms": latency["med"],
        "p95_ms": latency["p(95)"], "max_ms": latency["max"],
        "error_pct": errors["rate"] * 100, "errors": errors["passes"],
        "dropped": dropped["count"], "completed": completed,
        "actual_rps": metrics["iterations"]["values"]["rate"],
        "pass": latency["p(95)"] < 500 and errors["rate"] < 0.01,
    }
    return result


def step(out, api, db, password, mode, rate, seconds, label, profile_seconds=0):
    postfix = out / label
    name = lambda suffix: postfix.with_suffix("." + suffix)
    log_since = utc_now()
    wait_for_idle(api, db)
    seed = json.loads(compose("exec", "-T", "api", "python", "seed.py", "work"))
    name("seed.json").write_text(
        json.dumps({k: seed[k] for k in ("prefix", "dataset", "counts", "postgres")},
                   ensure_ascii=False, indent=2), encoding="utf-8",
    )
    idle = wait_for_idle(api, db)
    print(f"{utc_now()}  {label}  {rate} зап/с, {seconds} с, CPU до ступени {idle}",
          flush=True)

    stop = threading.Event()
    with subprocess.Popen(
        ["docker", "stats", "--format", "{{json .}}", api, db],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    ) as stats:
        stats_thread = threading.Thread(target=docker_stats, args=(stats, name("stats.jsonl")))
        pg_thread = threading.Thread(target=pg_activity, args=(stop, db, name("pg.jsonl")))
        stats_thread.start()
        pg_thread.start()
        try:
            start = utc_now()
            command = [
                "docker", "run", "--rm", "--network", f"container:{api}", "--user", "0",
                "-v", f"{LOAD}:/test:ro", "-v", f"{out}:/out",
                "-e", "SEED_PASSWORD", "-e", f"MODE={mode}", "-e", f"RATE={rate}",
                "-e", f"DURATION={seconds}s", "-e", "BASE_URL=http://127.0.0.1:8000",
                "-e", f"SUMMARY_PATH=/out/{label}.summary.json", K6_IMAGE, "run",
                "--quiet", "--out", f"json=/out/{label}.raw.json.gz", "/test/dz2.js",
            ]
            with name("k6.log").open("w", encoding="utf-8") as log:
                if profile_seconds:
                    with subprocess.Popen(
                        command, cwd=REPO, env={**os.environ, "SEED_PASSWORD": password},
                        stdout=log, stderr=subprocess.STDOUT,
                    ) as load:
                        time.sleep(5)
                        if load.poll() is not None:
                            raise RuntimeError(f"k6 остановился. См. {name('k6.log')}")
                        profile_command = [
                            "docker", "run", "--rm", "--network", "none", "--pid",
                            f"container:{api}", "--cap-add=SYS_PTRACE", "-v", f"{out}:/out",
                            "--entrypoint", "py-spy", PROFILER_IMAGE, "record", "--pid", "1",
                            "--duration", str(profile_seconds), "--rate", "100", "--nonblocking",
                            "--format", "raw", "--output", f"/out/{label}.profile.txt",
                        ]
                        with name("profile.log").open("w", encoding="utf-8") as profile_log:
                            profile = subprocess.run(profile_command, stdout=profile_log,
                                                     stderr=subprocess.STDOUT)
                        code = load.wait()
                    if profile.returncode:
                        raise RuntimeError(f"py-spy остановился. См. {name('profile.log')}")
                else:
                    code = subprocess.run(
                        command, cwd=REPO, env={**os.environ, "SEED_PASSWORD": password},
                        stdout=log, stderr=subprocess.STDOUT,
                    ).returncode
            end = utc_now()
        finally:
            stop.set()
            stats.terminate()
            stats_thread.join()
            pg_thread.join()
    if code not in (0, 99):
        raise RuntimeError(f"k6: код {code}. См. {name('k6.log')}")

    journal = run("docker", "logs", "--timestamps", "--since", log_since, api,
                  capture_output=True)
    name("api.log").write_text(journal.stdout + journal.stderr, encoding="utf-8")
    result = read_step(name("summary.json"), rate, seconds)
    result = {"label": label, "mode": mode, "start_utc": start, "end_utc": end,
              "k6_code": code, **result}
    name("step.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    decision = "пройден" if result["pass"] else "нарушен"
    print(f"  p50 {result['median_ms']:.1f} мс, p95 {result['p95_ms']:.1f} мс, "
          f"ошибки {result['error_pct']:.2f} %, пропущено {result['dropped']}, "
          f"факт {result['actual_rps']:.2f} зап/с, порог {decision}", flush=True)
    return result


def sweep(out, api, db, password, mode, start, maximum, seconds):
    results = []
    rate = start
    while rate <= maximum:
        row = step(out, api, db, password, mode, rate, seconds, f"{mode}-{rate}")
        results.append(row)
        if not row["pass"]:
            if len(results) > 1 and results[0]["pass"]:
                return results
            if len(results) >= 3 and row["actual_rps"] <= results[-2]["actual_rps"] * 1.10:
                return results
        rate *= 2
    return results


def write_csv(out):
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in out.glob("*.step.json")]
    rows.sort(key=lambda row: (row["start_utc"], row["label"]))
    if rows:
        with (out / "steps.csv").open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--stage", choices=("all", "mixed", "heavy", "one", "smoke"), default="all")
    parser.add_argument("--mode", choices=("mixed", "heavy"), default="mixed")
    parser.add_argument("--rate", type=int, default=2)
    parser.add_argument("--max-rate", type=int, default=512)
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--tuning", action="store_true")
    parser.add_argument("--label")
    args = parser.parse_args()
    if args.rate < 1 or args.duration < 1 or args.max_rate < args.rate:
        parser.error("требуются положительные значения RATE, DURATION и MAX_RATE ≥ RATE")
    if args.profile and (args.stage != "one" or args.mode != "heavy" or args.duration < 25):
        parser.error("профиль доступен для одной тяжёлой ступени длительностью от 25 секунд")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    print(f"Стенд {PROJECT}. Запись замеров в {out}.", flush=True)
    api = compose("ps", "-q", "api")
    db = compose("ps", "-q", "db")
    if not api or not db:
        parser.error("запустите изолированный стенд по README")
    wait_for_http(api)
    password = docker("exec", api, "printenv", "SEED_PASSWORD")
    context = {
        "prefix": "rasul_zeynalov", "project": PROJECT, "account": "rasul", "utc": utc_now(),
        "host": run("uname", "-a", capture_output=True).stdout.strip(),
        "cpu": run("lscpu", capture_output=True).stdout.strip(),
        "memory": run("free", "-m", capture_output=True).stdout.strip(),
        "docker": docker("version", "--format", "{{.Server.Version}}"),
        "compose": docker("compose", "version", "--short"),
        "python": docker("exec", api, "python", "--version"),
        "k6": docker("run", "--rm", K6_IMAGE, "version"),
        "image": docker("inspect", "--format", "{{.Image}}", api),
        "app_sha256": docker("exec", api, "sha256sum", "app.py"),
        "scenario_sha256": run("sha256sum", "load/dz2.js", capture_output=True).stdout.split()[0],
    }
    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True)
    if git.returncode == 0:
        context["commit"] = git.stdout.strip()
    (out / "context.json").write_text(json.dumps(context, indent=2), encoding="utf-8")
    try:
        if args.stage == "smoke":
            step(out, api, db, password, "mixed", 5, 6, "smoke")
            return
        if args.stage == "one":
            label = args.label or f"{args.mode}-{args.rate}-repeat"
            step(out, api, db, password, args.mode, args.rate, args.duration, label,
                 profile_seconds=15 if args.profile else 0)
            return

        mixed = []
        if args.stage in ("all", "mixed"):
            mixed = sweep(out, api, db, password, "mixed", args.rate, args.max_rate, args.duration)
        if args.stage in ("all", "heavy"):
            heavy = sweep(out, api, db, password, "heavy", args.rate, args.max_rate, args.duration)
            preceding = [row for row in heavy if row["pass"]]
            profile_rate = preceding[-1]["rate"] if preceding else heavy[-1]["rate"]
            step(out, api, db, password, "heavy", profile_rate, max(30, args.duration),
                 f"profile-heavy-{profile_rate}", profile_seconds=15)
            (out / "profiler-version.txt").write_text(
                docker("run", "--rm", "--network", "none", "--entrypoint", "py-spy",
                       PROFILER_IMAGE, "--version") + "\n", encoding="utf-8",
            )

        breaker = next((row for row in mixed if not row["pass"]), None)
        if args.tuning and breaker:
            rate = breaker["rate"]
            try:
                compose("up", "-d", "--no-deps", "--no-build", "--force-recreate", "api", workers=2)
                api = compose("ps", "-q", "api", workers=2)
                wait_for_http(api)
                step(out, api, db, password, "mixed", rate, args.duration, f"tuning-{rate}-w2")
            finally:
                compose("up", "-d", "--no-deps", "--no-build", "--force-recreate", "api", workers=1)
    finally:
        write_csv(out)


if __name__ == "__main__":
    main()
