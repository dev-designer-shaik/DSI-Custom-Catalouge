#!/usr/bin/env bash
# Push the shipping-policy files to the erp2 sandbox and load them there.
#
#   tools/erp2_push.sh [--restart] [--dry-run]
#
# erp2's copy of the catalogue app is an old checkout that is not a git repo: the folder
# ~/dsi_catalogue_dev on the host, mounted into the backend container as apps/dsi_catalogue. This script is
# the one way files reach it. It
#
#   1. reads tools/shipping_policy_files.txt (one repo-relative path per line; blank lines and # comments
#      are ignored) and refuses the whole run, before anything is sent, if a path is outside the allow-list
#      below, is missing, is not a plain file, or is reached through a link (a link to the file, or to a
#      folder on the way to it);
#   2. copies those files, and only those, to erp2-sandbox:~/dsi_catalogue_dev/<path> with rsync -R (what is
#      already identical is skipped; nothing on erp2 is ever deleted);
#   3. reloads every listed doctype from its JSON (the child tables first, then the zone, the item set, the
#      rule and the policy), then the wizard page when it is listed. A doctype whose JSON is unchanged is
#      skipped;
#   4. clears the site cache.
#
#   --restart  also restarts the backend container, so that its running web workers load changed Python
#   --dry-run  checks the list and prints every step; nothing is copied, nothing is run, no connection is made
#
# Running it twice changes nothing the second time. It never runs a migration, never touches another app,
# site or container, and restarts only the backend container, and only when asked.
#
# Step 3 does what `bench reload-doc dsi_catalogue doctype <name>` does (it calls frappe.reload_doc), but for
# every listed doctype in one small Python process of its own: one connection to the site, a commit after each
# reload, and a line for each saying whether it was loaded or was already current, which the bench command
# does not print. Until 2026-10-22 the bench command could not be used on erp2 at all: bench maps a module to
# its app through sites/apps.txt, and that file did not list dsi_catalogue, so the command failed with "Module
# dsi_catalogue not found". The file has been fixed since. The process still adds the modules of the app being
# reloaded to its own module map first, which changes nothing now and keeps the script working on a site whose
# apps.txt lacks the app.

# The commands sent over ssh are built here on purpose, from constants and from names that passed the allow-list.
# shellcheck disable=SC2029
set -euo pipefail
set -o errtrace

HOST="erp2-sandbox"           # ssh alias of the sandbox (ubuntu@13.203.74.249)
REMOTE_ROOT="dsi_catalogue_dev" # the checkout, relative to the ssh user's home
CONTAINER="frappe_docker-backend-1"
BENCH_DIR="/home/frappe/frappe-bench"
SITE="erp.shaik.net"
APP="dsi_catalogue"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20)

# Children first (a table must exist before its parent points at it), then the doctypes that link to each
# other: the rule links to a zone and an item set, and the policy holds the rules.
RELOAD_ORDER=(
	shipping_zone_country
	shipping_item_set_group
	shipping_item_set_item
	shipping_zone
	shipping_item_set
	shipping_policy_rule
	shipping_policy
)

# The only paths that may be sent: a file directly in one of these places, with a plain name.
ALLOWED=(
	'^dsi_catalogue/shipping_[A-Za-z0-9_.-]+$'
	'^dsi_catalogue/dsi_catalogue/doctype/shipping_[A-Za-z0-9_]+/[A-Za-z0-9_][A-Za-z0-9_.-]*$'
	'^dsi_catalogue/dsi_catalogue/page/shipping_policy_wizard/[A-Za-z0-9_][A-Za-z0-9_.-]*$'
	'^tests/hermetic/fixtures/shipping_policy_example\.json$'
)
DOCTYPE_JSON='^dsi_catalogue/dsi_catalogue/doctype/([a-z0-9_]+)/([a-z0-9_]+)\.json$'
PAGE_JSON='^dsi_catalogue/dsi_catalogue/page/shipping_policy_wizard/shipping_policy_wizard\.json$'

# What runs inside the container (see step 3 above). It is read from stdin; the site, the app and the
# kind:name pairs to reload arrive as arguments, so nothing listed is ever part of the code.
read -r -d '' RELOAD_PY <<'PY' || true
import sys

import frappe

site, app, *reloads = sys.argv[1:]
frappe.init(site=site, sites_path=".")
frappe.connect()
try:
	# A safety net: a no-op on a site whose apps.txt lists the app (erp2's has since 2026-10-22), and what lets
	# the reload work on one whose apps.txt does not.
	for module in frappe.get_module_list(app):
		frappe.local.module_app.setdefault(frappe.scrub(module), app)
	for item in reloads:
		kind, name = item.split(":")
		loaded = frappe.reload_doc(app, kind, name)  # the app's module has the app's name
		frappe.db.commit()
		print(f"  {kind} {name}: {'loaded' if loaded else 'already current'}")
finally:
	frappe.destroy()
PY

# The real path (pwd -P): each listed file is checked against it below, and a repository reached through a
# link would otherwise make every file look as if it were somewhere else.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
LIST="$ROOT/tools/shipping_policy_files.txt"
STEP="reading the file list"

RESTART=0
DRY_RUN=0

usage() {
	echo "Usage: tools/erp2_push.sh [--restart] [--dry-run]"
}

say() {
	echo "erp2_push: $*"
}

trap 'echo "erp2_push: failed while: $STEP" >&2' ERR

for arg in "$@"; do
	case $arg in
		--restart) RESTART=1 ;;
		--dry-run) DRY_RUN=1 ;;
		-h | --help)
			usage
			exit 0
			;;
		*)
			usage >&2
			exit 2
			;;
	esac
done

# ---------------------------------------------------------------- 1. the list
allowed() {
	local pattern
	for pattern in "${ALLOWED[@]}"; do
		[[ $1 =~ $pattern ]] && return 0
	done
	return 1
}

files=()
doctypes=()
declare -A listed=()
page=0
refused=0

[[ -f $LIST ]] || {
	echo "erp2_push: refused: the file list $LIST is missing" >&2
	exit 1
}

while IFS= read -r line || [[ -n $line ]]; do
	line="${line%$'\r'}"
	line="${line#"${line%%[![:space:]]*}"}"
	line="${line%"${line##*[![:space:]]}"}"
	[[ -z $line || $line == \#* ]] && continue
	# Only a line that passed the allow-list is ever used as anything but text.
	if ! allowed "$line"; then
		echo "erp2_push: refused: $line (not inside the allow-list)" >&2
		refused=1
	elif [[ -L $ROOT/$line ]]; then
		echo "erp2_push: refused: $line (a link, not a plain file)" >&2
		refused=1
	elif [[ ! -f $ROOT/$line ]]; then
		echo "erp2_push: refused: $line (not a file in this repository)" >&2
		refused=1
	elif [[ $(realpath -e -- "$ROOT/$line" 2>/dev/null) != "$ROOT/$line" ]]; then
		# -L above only looks at the last part of the path: a link to a folder in front of it would send a file
		# from wherever the link leads, under the name of one inside the repository.
		echo "erp2_push: refused: $line (reached through a link, so not a plain file in this repository)" >&2
		refused=1
	elif [[ -z ${listed[$line]:-} ]]; then
		listed[$line]=1
		files+=("$line")
		if [[ $line =~ $DOCTYPE_JSON && ${BASH_REMATCH[1]} == "${BASH_REMATCH[2]}" ]]; then
			doctypes+=("${BASH_REMATCH[1]}")
		elif [[ $line =~ $PAGE_JSON ]]; then
			page=1
		fi
	fi
done <"$LIST"

if ((refused)); then
	echo "erp2_push: nothing was sent." >&2
	exit 1
fi
if ((${#files[@]} == 0)); then
	echo "erp2_push: refused: the file list names no files" >&2
	exit 1
fi

# The doctypes in the order they are reloaded: the known ones in RELOAD_ORDER, then any other listed doctype
# folder alphabetically, so that nothing listed is skipped. The page comes last.
reloads=()
for name in "${RELOAD_ORDER[@]}"; do
	for found in "${doctypes[@]}"; do
		if [[ $found == "$name" ]]; then
			reloads+=("doctype:$name")
		fi
	done
done
if ((${#doctypes[@]})); then
	while IFS= read -r found; do
		if [[ " ${RELOAD_ORDER[*]} " != *" $found "* ]]; then
			reloads+=("doctype:$found")
		fi
	done < <(printf '%s\n' "${doctypes[@]}" | sort -u)
fi
if ((page)); then
	reloads+=("page:shipping_policy_wizard")
fi

if ((DRY_RUN)); then
	say "dry run: nothing is copied and nothing is run"
fi
say "${#files[@]} file(s) listed, all inside the allow-list"

# ---------------------------------------------------------------- 2. copy
STEP="copying the files to $HOST"
echo "copy to $HOST:~/$REMOTE_ROOT/ (rsync -R --checksum; nothing there is deleted):"
python_updated=0
if ((DRY_RUN)); then
	printf '  %s\n' "${files[@]}"
else
	copied=0
	copy_log="$(cd "$ROOT" && rsync -R -c --chmod=D755,F644 --itemize-changes \
		-e "ssh ${SSH_OPTS[*]}" -- "${files[@]}" "$HOST:$REMOTE_ROOT/")"
	# rsync itemizes a file it sends as "<f+++++++++ path" when the file is new and "<fc....... path" when its
	# content changed (">f" when it receives one). Directories and unchanged files are not reported as files.
	while IFS= read -r line; do
		case $line in
			[\<\>]'f+++++++++ '*)
				echo "  new      ${line#* }"
				copied=$((copied + 1))
				;;
			[\<\>]'f'*)
				echo "  updated  ${line#* }"
				copied=$((copied + 1))
				if [[ ${line#* } == *.py ]]; then
					python_updated=1
				fi
				;;
		esac
	done <<<"$copy_log"
	say "$copied of ${#files[@]} file(s) copied, the rest were already current"
fi

# ---------------------------------------------------------------- 3 and 4. load
echo "run on $HOST (inside $CONTAINER):"
STEP="reloading ${reloads[*]}"
if ((DRY_RUN)); then
	for item in "${reloads[@]}"; do
		echo "  reload-doc $APP ${item%%:*} ${item#*:}"
	done
elif ((${#reloads[@]})); then
	ssh "${SSH_OPTS[@]}" "$HOST" \
		"docker exec -i $CONTAINER bash -c 'cd $BENCH_DIR/sites && $BENCH_DIR/env/bin/python - $SITE $APP ${reloads[*]}'" \
		<<<"$RELOAD_PY"
fi

STEP="clearing the cache"
echo "  bench --site $SITE clear-cache"
if ((!DRY_RUN)); then
	ssh -n "${SSH_OPTS[@]}" "$HOST" "docker exec $CONTAINER bash -c 'cd $BENCH_DIR && bench --site $SITE clear-cache'"
fi

# ---------------------------------------------------------------- restart, only when asked
if ((RESTART)); then
	STEP="restarting $CONTAINER"
	restart="docker restart $CONTAINER"
	echo "  $restart"
	if ((!DRY_RUN)); then
		ssh -n "${SSH_OPTS[@]}" "$HOST" "$restart"
	fi
elif ((python_updated)); then
	say "NOTE: a Python file already on erp2 changed. Its running web workers keep the old code until the"
	say "backend restarts: run this again with --restart (nothing else is copied the second time)."
fi

if ((DRY_RUN)); then
	say "dry run complete"
else
	say "done"
fi
