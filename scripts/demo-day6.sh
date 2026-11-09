#!/usr/bin/env bash
#
# 第六天「Redis + ARQ 自动生成日报」的八场景演示脚本。
#
# 为什么要有它：评审现场敲命令最容易出错，也容易漏看。这里把每条场景的
# 动作、实际输出、期望值放在一起，跑完就能对着看。
#
# 用法：
#   bash scripts/demo-day6.sh 2      # 只跑场景 2
#   bash scripts/demo-day6.sh all    # 依次跑（需要停服务/改配置的会打印指令，不自动执行）
#
# 约定：
#   · 只读校验自动跑；会影响环境的（改 cron、停 worker、停 redis）只打印指令 ——
#     演示时现场执行，避免脚本把演示环境改坏。
#   · 每个场景先打印「期望」，再打印「实际」，最后给 PASS/FAIL；
#     有 FAIL 时脚本以退出码 1 结束，方便一眼看出没通过。

set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

COMPOSE="docker compose"
PY="$COMPOSE exec -T backend python"
DAY="${DEMO_DAY:-$(date -d 'yesterday' +%F)}"
FAILED=0

green() { printf '\033[32m%s\033[0m\n' "$*"; }
red() { printf '\033[31m%s\033[0m\n' "$*"; }
blue() { printf '\033[36m%s\033[0m\n' "$*"; }
title() {
  echo
  echo "================================================================"
  blue "场景 $1：$2"
  echo "================================================================"
}

# 从 row 输出里取一个字段：field "status=ready planned=5" planned -> 5
field() {
  printf '%s\n' "$1" | tr ' ' '\n' | sed -n "s/^$2=//p"
}

# expect <字段名> <实际值> <期望值>
expect() {
  if [ "$2" = "$3" ]; then
    green "  PASS  $1 = $3"
  else
    red "  FAIL  $1 期望=$3 实际=$2"
    FAILED=$((FAILED + 1))
  fi
}

# 只看日报那一行的四个数字与状态，输出成一行便于比对
row() {
  $PY - "$1" "$2" <<'PY'
import sys
from app.db import get_conn
name, day = sys.argv[1], sys.argv[2]
with get_conn() as c:
    u = c.execute("SELECT id FROM users WHERE username = ?", (name,)).fetchone()
    if u is None:
        print(f"无此用户：{name}（先跑 scripts/seed.py 建号）")
        raise SystemExit(0)
    r = c.execute(
        "SELECT status, planned_total, completed, skipped, unfinished, postpone_ops,"
        " sedentary_minutes, source FROM daily_summaries"
        " WHERE user_id = ? AND report_date = ?",
        (int(u["id"]), day),
    ).fetchone()
if r is None:
    print("missing")
else:
    sedentary = "null" if r["sedentary_minutes"] is None else r["sedentary_minutes"]
    print(
        f"status={r['status']} planned={r['planned_total']} completed={r['completed']} "
        f"skipped={r['skipped']} unfinished={r['unfinished']} postpone_ops={r['postpone_ops']} "
        f"sedentary={sedentary} source={r['source']}"
    )
PY
}

step1() {
  title 1 "自动执行（cron 到点自己跑，不点按钮）"
  cat <<'TXT'
动作（手工，会让 worker 每两分钟触发一次）：
  1) 在 .env 里加两行：
       APP_SUMMARY_CRON_HOUR=*
       APP_SUMMARY_CRON_MINUTE=*/2
  2) docker compose up -d --force-recreate worker
  3) docker compose logs -f worker     # 等一个偶数分钟

期望日志：
  调度完成 date=... 有记录用户=N 新入队=N
  日报生成完成 user=1 date=... source=llm wrote=True
然后用 `bash scripts/demo-day6.sh 1` 复查上面那条已经变成 ready。

演示完记得把这两行删掉，恢复每天 00:05。
TXT
  echo "当前日报状态（若已生成应看到 status=ready）："
  row demo "$DAY"
}

step2() {
  title 2 "统计正确（造一天的记录，四个数字要对上）"
  echo "1) 造 D=$DAY 的记录（脚本会先清掉这一天的活动与旧日报，可重复执行）："
  $COMPOSE exec -T backend python scripts/seed_activity_demo.py --days-ago 1 2>&1 | tail -12

  echo
  echo "2) 触发一次生成，等它变 ready："
  $PY - "$DAY" <<'PY'
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
day = sys.argv[1]


def call(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


token = call("POST", "/auth/login", {"username": "demo", "password": "demo1234"})["token"]
got = call("POST", f"/summary/run?day={day}", token=token)
if got["status"] == "deduplicated":
    # 同一分钟内重复触发会被分钟桶去重。seed 刚把旧日报删了，任务不会补跑，
    # 所以这里主动跨过分钟边界再触发一次，否则会一直等成 missing。
    wait = 61 - int(time.time()) % 60
    print(f"   deduplicated（这一分钟已经提交过），等 {wait}s 跨过分钟边界再触发")
    time.sleep(wait)
    got = call("POST", f"/summary/run?day={day}", token=token)
print(f"   触发返回 status={got['status']} queued={got['queued']}")

for _ in range(30):
    state = call("GET", f"/summary?day={day}", token=token)
    if state["status"] == "ready":
        print("   轮询结束：status=ready")
        break
    time.sleep(1)
else:
    print(f"   等待超时，当前状态 = {state['status']}（看 docker compose logs worker）")
PY

  echo
  echo "3) 期望：planned=5  completed=3  skipped=1  unfinished=2  postpone_ops=1"
  echo "   实际（库里那一行）："
  local got
  got=$(row demo "$DAY")
  echo "   $got"
  echo
  expect planned "$(field "$got" planned)" 5
  expect completed "$(field "$got" completed)" 3
  expect skipped "$(field "$got" skipped)" 1
  expect unfinished "$(field "$got" unfinished)" 2
  expect postpone_ops "$(field "$got" postpone_ops)" 1
  echo
  echo "   注意 unfinished(2) + completed(3) + skipped(1) = 6 > planned(5)："
  echo "   推迟会新建替代条，所以几个数不能相加 —— 这是设计如此，不是统计错。"
}

step3() {
  title 3 "无记录的账号（不能显示「久坐 0 分钟」）"
  cat <<'TXT'
动作（手工）：
  1) docker compose exec -T -e SEED_USERNAME=demo2 -e SEED_PASSWORD=demo1234 \
       backend python scripts/seed.py

  注意变量要用 docker compose exec 的 -e 传进容器；写在 docker 前面的
  SEED_USERNAME=... 只会留在宿主机 shell 里，容器读不到。

  2) 页面上用 demo2 登录 → 每日总结 → 立即生成

期望：页面显示"这一天没有排过任何活动"，四个数字都是 0，
      API 里 sedentary_minutes 是 null（不是 0）。
TXT
  echo "当前 demo2 的日报（若已建号并生成）："
  row demo2 "$DAY"
}

step4() {
  title 4 "重复执行（连点多次，库里只能有一行）"
  echo "1) 先清掉这一天已有的日报 —— 否则第一天就命中「已有日报直接复用」，"
  echo "   连点根本走不到入队那一步，看不到去重："
  $PY - "$DAY" <<'PY'
import sys
from app.db import get_conn
with get_conn() as c:
    n = c.execute(
        "DELETE FROM daily_summaries"
        " WHERE user_id = (SELECT id FROM users WHERE username = 'demo')"
        " AND report_date = ?",
        (sys.argv[1],),
    ).rowcount
print(f"   已清掉 {n} 行")
PY

  echo
  echo "2) 同一分钟内连投三次同一个日期："
  echo "   分钟桶是「同一分钟内只算一次」。上一个场景很可能已经在这一分钟"
  echo "   提交过，不跨过边界就会直接命中 deduplicated，看不到 queued。"
  local wait
  wait=$((61 - 10#$(date +%S)))
  echo "   等待 ${wait}s 跨过分钟边界…"
  sleep "$wait"
  local out res
  out=$($PY - "$DAY" <<'PY'
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
day = sys.argv[1]


def call(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


token = call("POST", "/auth/login", {"username": "demo", "password": "demo1234"})["token"]
seen = []
for i in range(3):
    got = call("POST", f"/summary/run?day={day}", token=token)
    seen.append(got["status"])
    print(f"  第 {i + 1} 次：queued={got['queued']} status={got['status']} job={got['job_id']}")
    if i < 2:
        time.sleep(1)
print(f"RESULT first={seen[0]} second={seen[1]} third={seen[2]}")
PY
)
  printf '%s\n' "$out" | grep -v '^RESULT '
  res=$(printf '%s\n' "$out" | grep '^RESULT ' | tail -1)

  echo
  echo "3) 期望：第 1 次 queued（真入队）；"
  echo "        第 2、3 次一定不是 queued —— worker 已经接手就是 generating，"
  echo "        还在队列里就是 deduplicated（被分钟桶挡住）；"
  echo "        无论点几次，库里同一 (user_id, report_date) 只有 1 行"
  expect "第 1 次" "$(field "${res#RESULT }" first)" queued
  local k v
  for k in second third; do
    v=$(field "${res#RESULT }" "$k")
    case "$v" in
      deduplicated | generating | ready)
        green "  PASS  第 $([ "$k" = second ] && echo 2 || echo 3) 次 = $v（不是 queued，没有重复入队）"
        ;;
      *)
        red "  FAIL  第 $k 次 = $v（期望不是 queued）"
        FAILED=$((FAILED + 1))
        ;;
    esac
  done
  local n
  n=$($PY - "$DAY" <<'PY'
import sys
from app.db import get_conn
with get_conn() as c:
    print(c.execute(
        "SELECT COUNT(*) FROM daily_summaries"
        " WHERE user_id = (SELECT id FROM users WHERE username = 'demo')"
        " AND report_date = ?",
        (sys.argv[1],),
    ).fetchone()[0])
PY
)
  echo "  实际行数 = $n"
  expect "日报行数" "$n" 1
}

step5() {
  title 5 "失败与恢复（模型挂掉时有限重试，修好后能补跑）"
  cat <<'TXT'
动作（手工）：
  1) 把 .env 里的 OPENAI_API_KEY 改成一个错的，重启 backend 和 worker
  2) 页面上点「重试」，或调 POST /summary/run?day=<某天>
  3) docker compose logs worker   观察 retrying → "max retries 2 exceeded"

期望：
  · 日志里能看到有限次重试（APP_SUMMARY_MAX_TRIES=3，含首次）
  · 接口 GET /summary 返回 status=failed、error_code=transient
  · 把 key 改回来再点一次，能生成成功，error_* 字段被清空

注意：真模型模式下 401 会被判成可重试错误；配置缺失（没有 key）是永久错误，
      一次就落 failed —— 这两类在这里的日志不一样。
TXT
}

step6() {
  title 6 "Worker 暂停（任务在队列里等，起来后立刻执行）"
  cat <<'TXT'
动作（手工）：
  1) docker compose stop worker
  2) 页面点「立即生成」→ 接口返回 queued=true，但 GET /summary 一直是 missing
  3) docker compose start worker
  4) 看日志里 "delayed=..." 这一项，然后页面刷新

期望：任务没有丢，worker 起来后马上被领取；中途 restart 也不会产生第二份日报
      （靠 UNIQUE(user_id, report_date)，不靠队列不重复）。
TXT
}

step7() {
  title 7 "持久化与权限（重启后还在；换个账号看不到别人的）"
  echo "重启 backend 后重新查（重启期间接口会短暂不通，脚本内部重试）："
  $COMPOSE restart backend >/dev/null 2>&1
  $PY - "$DAY" <<'PY'
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
day = sys.argv[1]


def call(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


# 容器刚重启时 8000 还没监听，先等它起来再查
for attempt in range(30):
    try:
        call("POST", "/auth/login", {"username": "demo", "password": "demo1234"})
        break
    except (urllib.error.URLError, ConnectionError):
        time.sleep(1)

for name in ("demo", "demo2"):
    try:
        token = call("POST", "/auth/login", {"username": name, "password": "demo1234"})["token"]
        got = call("GET", f"/summary?day={day}", token=token)
        print(f"  {name:6} -> status={got['status']} completed={got['completed']}")
    except Exception as exc:  # 账号不存在就跳过
        print(f"  {name:6} -> 查询失败：{exc}")
PY
  echo "期望：demo 仍是 ready 且有数字；demo2 看到的是它自己的 missing（不是别人的日报）"
}

step8() {
  title 8 "Redis 异常（查询照常，触发按钮明确报错）"
  cat <<'TXT'
动作（手工）：
  1) docker compose stop redis
  2) 页面刷新 → GET /summary 仍然是 200，已生成的日报照常显示
  3) 点「立即生成」→ 期望 HTTP 503，提示"任务队列暂不可用，请稍后再试"
  4) docker compose start redis

关键点：Redis 是可选依赖。它挂了只影响"触发"这一个动作，
      查询、登录、活动列表都不受影响 —— 所以连接池是惰性创建的。
TXT
}

main() {
  local target="${1:-help}"
  case "$target" in
    1) step1 ;;
    2) step2 ;;
    3) step3 ;;
    4) step4 ;;
    5) step5 ;;
    6) step6 ;;
    7) step7 ;;
    8) step8 ;;
    all)
      step1
      step2
      step4
      step7
      echo
      blue "场景 3/5/6/8 需要停服务或改配置，已跳过。逐个执行："
      echo "  bash scripts/demo-day6.sh 3"
      echo "  bash scripts/demo-day6.sh 5"
      echo "  bash scripts/demo-day6.sh 6"
      echo "  bash scripts/demo-day6.sh 8"
      ;;
    *)
      sed -n '3,16p' "$0"
      echo
      echo "可选：1 2 3 4 5 6 7 8 all"
      ;;
  esac

  echo
  if [ "$FAILED" -eq 0 ]; then
    green "全部通过（脚本只校验能只读断言的项；带「手工」的场景需要照着指令自己看）"
  else
    red "有 $FAILED 项未通过，往上翻 FAIL 行"
    exit 1
  fi
}

main "${1:-help}"
