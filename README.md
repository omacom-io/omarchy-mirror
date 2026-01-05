# Omarchy Mirror

Dedicated mirror of Arch core/extra/multilib repositories for Omarchy hosted on Cloudflare R2.

## Stable vs edge

The stable mirror is the default for Omarchy. It's located at `https://stable-mirror.omarchy.org/`, and it typically runs one month behind the very latest. This allows the Omarchy team time to catch any incompatibilities with new libraries or tools, so that problems can be fixed before they're rolled out to everyone. The mirror may be updated more frequently as needed to address security issues or general releases.

The edge mirror tracks the very latest Arch repositories every hour. It's located at `https://mirror.omarchy.org/`. Omarchy users can switch to this using _Update > Channel > Edge_ in the Omarchy Menu.
