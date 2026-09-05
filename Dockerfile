# One image, three entry points. The worker and the scheduler are the same build with
# different commands, so a deployment can never run mismatched versions of the two, and a
# job's diagnostics always refer to code that is actually installed here.
#
# Migrations are deliberately not run at startup. They are a separate release task using
# this same image: `python -m nordicintel_core.database migrate upgrade head`. Running
# them from a worker would mean every replica racing to change the schema it depends on.
FROM python:3.12-slim-bookworm AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /src
# The lockfile is copied first so a source edit does not invalidate the dependency layer,
# and --frozen means the image can only be built from a lockfile that is already correct.
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# An unprivileged user with no home directory to write to: this process reads a queue and
# talks to one database, and needs nothing else.
RUN useradd --system --create-home --uid 10001 harvest
COPY --from=build /opt/venv /opt/venv
USER harvest

# Overridden with `nordicintel-scheduler` for the singleton process, and with
# `nordicintel-bootstrap ...` for one-off operator commands.
CMD ["nordicintel-worker"]
