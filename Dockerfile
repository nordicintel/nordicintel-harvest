# One image, three entry points. The worker and the scheduler are the same build with
# different commands, so a deployment can never run mismatched versions of the two, and a
# job's diagnostics always refer to code that is actually installed here.
#
# Migrations are deliberately not run at startup. They are a separate release task using
# this same image: `python -m nordicintel_core.database migrate upgrade head`. Running
# them from a worker would mean every replica racing to change the schema it depends on.
#
# Build it from the directory holding the checkouts, because the adapter packages a
# deployment serves are still sibling paths rather than releases:
#
#   docker build -f nordicintel-harvest/Dockerfile \
#     --secret id=gh_token,env=GH_TOKEN \
#     --build-arg EXTRAS=--all-extras \
#     -t nordicintel-harvest .
#
# Once every adapter has a tag of its own, the context is nordicintel-harvest alone.
FROM python:3.12-slim-bookworm AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

# nordicintel-core is pinned to a tag in a private repository, so resolving it needs git.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --uid 10001 harvest

# The checkout layout is mirrored, because [tool.uv.sources] resolves the adapter as a
# sibling of this directory.
WORKDIR /src/nordicintel-harvest
COPY nordicintel-adapter-pxweb2 /src/nordicintel-adapter-pxweb2
# The lockfile is copied first so a source edit does not invalidate the dependency layer,
# and --frozen means the image can only be built from a lockfile that is already correct.
COPY nordicintel-harvest/pyproject.toml nordicintel-harvest/uv.lock nordicintel-harvest/README.md ./
COPY nordicintel-harvest/src ./src

ARG EXTRAS=""
# The token is read from a build secret and passed to git through the environment, so it
# is never written to a file and never lands in a layer. GIT_CONFIG_COUNT applies the
# rewrite for the length of this command only.
RUN --mount=type=secret,id=gh_token \
    GIT_CONFIG_COUNT=1 \
    GIT_CONFIG_KEY_0="url.https://x-access-token:$(cat /run/secrets/gh_token)@github.com/.insteadOf" \
    GIT_CONFIG_VALUE_0="https://github.com/" \
    uv sync --frozen --no-dev --no-editable ${EXTRAS}

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# An unprivileged user: this process reads a queue and talks to one database, and needs
# nothing else. Copied rather than created so the uid matches what built /opt/venv.
COPY --from=build /etc/passwd /etc/passwd
COPY --from=build /opt/venv /opt/venv
USER harvest

# Overridden with `nordicintel-scheduler` for the singleton process, and with
# `nordicintel-bootstrap ...` for one-off operator commands.
CMD ["nordicintel-worker"]
