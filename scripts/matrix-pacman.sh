#!/bin/bash
# Client-behaviour matrix: drive a real pacman against the Worker while ring
# selections move underneath it. State persists across steps on purpose, so
# libalpm issues the conditional requests a returning client would issue.
set -u
WORKER=http://host.containers.internal:8787
CONTROL=http://host.containers.internal:8788
STABLE=79ee9437e3118931c2cbdf1e9db0df8e4bae8a7c884c6e80b15761b4d68f61c8
EDGE=b178a6516023e9475c5031d768b5de5b1e8e5b5688d309de745e0ad3720be335
ROOT=/state
rm -rf "$ROOT"; mkdir -p "$ROOT"/{db,cache,gnupg}; chmod 700 "$ROOT/gnupg"

FPR=$(python -c 'import json;print(json.load(open("/demo/result.json"))["signing_fingerprint"])')
gpg --no-options --homedir "$ROOT/gnupg" --batch --no-default-keyring \
    --keyring "$ROOT/gnupg/pubring.gpg" --import /demo/demo-keyring.gpg >/dev/null 2>&1
echo "$FPR:6:" | gpg --no-options --homedir "$ROOT/gnupg" --batch --no-default-keyring \
    --keyring "$ROOT/gnupg/pubring.gpg" --import-ownertrust >/dev/null 2>&1

cat > "$ROOT/pacman.conf" <<CONF
[options]
Architecture = x86_64
SigLevel = Required DatabaseRequired
DisableSandbox
[extra-stable]
Server = $WORKER/extra/os/\$arch
CONF

PAC=(fakeroot pacman --config "$ROOT/pacman.conf" --dbpath "$ROOT/db" --cachedir "$ROOT/cache"
     --gpgdir "$ROOT/gnupg" --logfile "$ROOT/pacman.log" --noconfirm)

fail=0
version() { "${PAC[@]}" -Sp --print-format '%v' -- pool-demo-app 2>/dev/null | tail -1; }
sync()    { "${PAC[@]}" -Sy >/dev/null 2>&1; }
activate() { curl -sS "$CONTROL/activate?ring=$1&build=$2&reason=$3" >/dev/null; sleep 1; }
step() { # name expected actual
  if [ "$2" = "$3" ]; then printf '  PASS  %-42s %s\n' "$1" "$3"
  else printf '  FAIL  %-42s expected %s, got %s\n' "$1" "$2" "$3"; fail=1; fi
}

echo "scenario: first sync from empty state"
sync; step "fresh install resolves stable" "1-1" "$(version)"

echo "scenario: second sync against an existing database"
sync; step "repeat sync keeps the same selection" "1-1" "$(version)"

echo "scenario: ring moves forward (stable <- edge selection)"
activate stable "$EDGE" promote
sync; step "client picks up the newer selection" "2-1" "$(version)"

echo "scenario: rollback (stable <- previous selection)"
activate stable "$STABLE" rollback
sync; step "client returns to the rolled back selection" "1-1" "$(version)"

echo "scenario: forced refresh recovers regardless"
"${PAC[@]}" -Syy >/dev/null 2>&1; step "-Syy agrees with the active ring" "1-1" "$(version)"

echo "scenario: package download follows the 307 into the pool"
"${PAC[@]}" -Syw --noprogressbar -- pool-demo-app >/dev/null 2>&1
ls "$ROOT/cache" | grep -q 'pool-demo-app-1-1' \
  && step "signed package fetched through redirect" "ok" "ok" \
  || step "signed package fetched through redirect" "ok" "missing"

gpgconf --homedir "$ROOT/gnupg" --kill gpg-agent >/dev/null 2>&1
exit $fail
