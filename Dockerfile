# promptx hosted server, containerized.
#
# Stdlib-only app, so the image is just Python plus four files — nothing to
# compile, nothing to pip install. That is deliberate: a NAS you do not
# control is still good enough, and the image cannot drift from the repo.
#
# Build (tag with the VERSION file so the image IS the release):
#   docker build --build-arg VERSION="$(cat VERSION)" -t "promptx:$(cat VERSION)" .
#
# The compose file pins the runtime uid/gid to match the NAS files the
# container reads; the user created here just keeps `docker run` sane on its
# own.

FROM python:3.12-slim

ARG VERSION=unknown
LABEL org.opencontainers.image.title="promptx" \
      org.opencontainers.image.description="Turns a vague request into an explicit work order" \
      org.opencontainers.image.source="https://github.com/NathanMaine/Promptx" \
      org.opencontainers.image.version="$VERSION"

RUN useradd --uid 1000 --user-group promptx
WORKDIR /app

# Hosted server plus its two modules and the version file. main.py / web.py
# are local-machine tools and do not belong in the image.
COPY server.py promptx_index.py promptx_version.py VERSION ./

# read_only rootfs in compose means no bytecode cache writes either.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER promptx
EXPOSE 7331

# /api/version has existed since 0.2.0 — it is both the health probe and the
# "what is actually running" answer.
CMD ["python3", "server.py", "--port", "7331", "--host", "0.0.0.0"]
