#!/usr/bin/env bash
# promote_item_sync.sh -- ship "Item edits sync to Website Item for everyone" to prod, then set one description.
#
#   ./tools/promote_item_sync.sh <erptest25|prod>    # does it (backup, install, restart, probe, description)
#
# Code change = 2 files of dsi_catalogue (website_item_review.py, hooks.py); no schema, no migrate.
# Fixed sequence (rule 13): gates (committed, hermetic tests) -> remote files == the commit this was built on ->
# copy the 2 prod files aside -> install + md5-check -> clear cache -> restart workers -> health -> probe (always
# rolled back; must say ALL PASS) -> description trial (rolled back) -> real description -> read back.
# Rollback of the code: put back the *.orig files from /home/ubuntu/dsi_catalogue_orig_<stamp> (docker cp), clear cache, restart.
# Rollback of the description: RESTORE_FROM=<snapshot printed below> python /tmp/set_item_description.py
set -euo pipefail
BOX="${1:-}"
case "$BOX" in
	prod)      KEY=/home/mdad/.ssh/erpProdMumbai.pem; HOST=ubuntu@3.108.58.67; SITE=erp.shaik.net ;;
	erptest25) KEY=/home/mdad/.ssh/historicalErp.pem; HOST=ubuntu@erptest25.shaik.net; SITE=erptest25.shaik.net ;;
	*) echo "usage: $0 <erptest25|prod>"; exit 2 ;;
esac
BK=frappe_docker-backend-1; APP=/home/frappe/frappe-bench/apps/dsi_catalogue/dsi_catalogue; B=/home/frappe/frappe-bench
ITEM="10000012-40ML"; DESC="OPULENT SHAIK NO.33 CLASSIC PARFUM WOMEN (40ML)"
BASE=702a036   # the commit prod's dsi_catalogue matches file for file
SRC="$(cd "$(dirname "$0")/.." && pwd)"; STAMP=$(date +%Y%m%d_%H%M%S); OUT="$SRC/tools/promote_logs"; mkdir -p "$OUT"
exec > >(tee -a "$OUT/item_sync_${BOX}_${STAMP}.log") 2>&1
R() { ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=15 "$HOST" "$@"; }
# every argument is %q-quoted: the remote shell re-parses ssh's command line (the description has spaces and parentheses)
PY() { local f="$1"; shift; local extra=""; [ $# -gt 0 ] && extra=$(printf '%q ' "$@")
	R "sudo docker exec -w $B/sites -e SITE=$SITE $extra $BK timeout 600 ../env/bin/python -u /tmp/$f" 2>&1 | grep -v RuntimeWarning; }
cd "$SRC"
echo "=== item sync -> $BOX $(date -Is) | HEAD $(git rev-parse --short HEAD)"
git diff --quiet HEAD -- . || { echo "REFUSING: working tree has uncommitted changes"; exit 1; }
FILES="dsi_catalogue/website_item_review.py dsi_catalogue/hooks.py"
python3 -m pytest -q tests/hermetic/test_item_sync.py tests/hermetic/test_auth.py tests/hermetic/test_index_key.py tests/hermetic/test_order_remarks.py 2>&1 | tail -1 | grep -q passed || { echo "REFUSING: hermetic tests fail"; exit 1; }
for f in $FILES; do
	REMOTE=$(R "sudo docker exec $BK md5sum $B/apps/dsi_catalogue/$f" | cut -d' ' -f1)
	[ "$(git show $BASE:$f | md5sum | cut -d' ' -f1)" = "$REMOTE" ] || [ "$(md5sum $f | cut -d' ' -f1)" = "$REMOTE" ] \
		|| { echo "REFUSING: the box's $f is neither the version this change was built on ($BASE) nor this change"; exit 1; }
done
echo "box files are $BASE (or already this change)"
R "mkdir -p /home/ubuntu/dsi_catalogue_orig_$STAMP && for f in website_item_review.py hooks.py; do sudo docker cp $BK:$APP/\$f /home/ubuntu/dsi_catalogue_orig_$STAMP/\$f.orig; done; ls /home/ubuntu/dsi_catalogue_orig_$STAMP"
scp -q -i "$KEY" tools/itemsync_probe.py tools/set_item_description.py $(for f in $FILES; do echo $f; done) "$HOST:/tmp/"
for f in itemsync_probe.py set_item_description.py; do R "sudo docker cp /tmp/$f $BK:/tmp/$f"; done
for f in website_item_review.py hooks.py; do
	R "sudo docker cp /tmp/$f $BK:$APP/$f"
	[ "$(md5sum dsi_catalogue/$f | cut -d' ' -f1)" = "$(R "sudo docker exec $BK md5sum $APP/$f" | cut -d' ' -f1)" ] || { echo "STOP: $f differs after copy"; exit 1; }
done
echo "installed + md5-checked"
R "sudo docker exec -w $B $BK bench --site $SITE clear-cache" | tail -1
R "sudo docker restart frappe_docker-backend-1 frappe_docker-frontend-1 frappe_docker-queue-short-1 frappe_docker-queue-long-1 frappe_docker-scheduler-1" 2>&1 | tail -5
for i in $(seq 1 30); do [ "$(curl -s -o /dev/null -w '%{http_code}' https://$SITE/api/method/ping)" = "200" ] && break; sleep 3; done
echo "ping: $(curl -s -o /dev/null -w '%{http_code}' https://$SITE/api/method/ping)"
R "sudo docker cp /tmp/itemsync_probe.py $BK:/tmp/itemsync_probe.py; sudo docker cp /tmp/set_item_description.py $BK:/tmp/set_item_description.py" >/dev/null
echo "##### PROBE (rolled back)"
PY itemsync_probe.py | tee "$OUT/item_sync_probe_${BOX}_${STAMP}.log"
grep -q "^ALL PASS" "$OUT/item_sync_probe_${BOX}_${STAMP}.log" || { echo "STOP: probe did not pass -- code is installed but the description was NOT changed. Roll back with the *.orig files."; exit 1; }
echo "##### DESCRIPTION trial"
PY set_item_description.py -e "ITEM=$ITEM" -e "DESCRIPTION=$DESC" -e DRY_ONLY=1 | tee "$OUT/item_desc_trial_${BOX}_${STAMP}.log"
grep -q "DRY_ONLY: verified" "$OUT/item_desc_trial_${BOX}_${STAMP}.log" || { echo "STOP: description trial failed"; exit 1; }
echo "##### DESCRIPTION real"
PY set_item_description.py -e "ITEM=$ITEM" -e "DESCRIPTION=$DESC" | tee "$OUT/item_desc_apply_${BOX}_${STAMP}.log"
grep -q "^DONE" "$OUT/item_desc_apply_${BOX}_${STAMP}.log" || { echo "STOP: description write failed"; exit 1; }
echo "=== DONE $(date -Is)"
