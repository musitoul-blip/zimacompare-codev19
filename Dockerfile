# =============================================================================
# ZimaCompare v19 — image AIO (All-In-One)
# S6-Overlay (PID 1) supervise : rclone + uvicorn + nginx dans un seul conteneur.
# Build sur la Zima : DOCKER_BUILDKIT=0 docker build -t ghcr.io/musitoul-blip/zimacompare-aio:v7 .
#
# Contexte de build attendu (même dossier que ce Dockerfile) :
#   ./Dockerfile
#   ./rootfs/      → arbo S6 + conf nginx + fuse.conf (copiée telle quelle dans l'image)
#   ./backend/     → sources Python v6 (réutilisées ; alignées au D-C)
#   ./frontend/    → sources React v6 (réutilisées ; lockfile à regénérer, cf. D-D)
# =============================================================================

# --- Étape 1 : build du frontend React (Vite) -------------------------------
FROM node:20-alpine AS frontend-build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json* ./
# package-lock.json v6 est vide (0 o) → npm ci échouerait ; fallback npm install.
# À regénérer au D-D pour un build 100% reproductible.
RUN npm ci || npm install
COPY frontend/ ./
COPY backend/config.py /tmp/config.py
RUN APP_VERSION="$(sed -nE 's/^APP_VERSION[^"]*"([^"]+)".*/\1/p' /tmp/config.py)" \
    && echo "BUILD APP_VERSION=$APP_VERSION" \
    && sed -i "s/\"version\": \"[^\"]*\"/\"version\": \"$APP_VERSION\"/" package.json \
    && VITE_APP_VERSION="$APP_VERSION" npm run build          # → /app/dist

# --- Étape 2 : image finale AIO ---------------------------------------------
FROM python:3.12-slim AS final

# Version S6 épinglée (réf. officielle just-containers, mai 2026). Modifiable.
ARG S6_OVERLAY_VERSION=3.2.3.0
# rclone : binaire officiel "current" (URL toujours valide). Épinglable si besoin.
ARG RCLONE_DOWNLOAD=https://downloads.rclone.org/v1.74.2/rclone-v1.74.2-linux-amd64.zip

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Europe/Paris \
    S6_KEEP_ENV=1 \
    S6_SERVICES_GRACETIME=20000 \
    S6_KILL_GRACETIME=6000 \
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2

# 1) Paquets système (nginx, fuse3, smartmontools pour smartinfo.py, util-linux…)
RUN apt-get update && apt-get install -y --no-install-recommends \
        nginx curl ca-certificates xz-utils unzip \
        fuse3 util-linux smartmontools tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/nginx/sites-enabled/default

# 2) S6-Overlay (noarch + x86_64 — ZimaBoard 2 = x86_64)
ADD https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz /tmp/
RUN tar -C / -Jxpf /tmp/s6-overlay-noarch.tar.xz
ADD https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-x86_64.tar.xz /tmp/
RUN tar -C / -Jxpf /tmp/s6-overlay-x86_64.tar.xz \
    && rm -f /tmp/s6-overlay-*.tar.xz

# 3) rclone (binaire officiel)
RUN curl -fsSL "${RCLONE_DOWNLOAD}" -o /tmp/rclone.zip \
    && unzip -q /tmp/rclone.zip -d /tmp \
    && cp /tmp/rclone-*-linux-amd64/rclone /usr/local/bin/rclone \
    && chmod 0755 /usr/local/bin/rclone \
    && rm -rf /tmp/rclone*

# 4) Dépendances Python (couche cachée tant que requirements.txt ne change pas)
#    NB : 'docker' sera retiré de requirements.txt au D-C (plus de socket Docker).
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 5) Code backend
COPY backend/ /app/
WORKDIR /app

# 6) Frontend compilé servi par nginx
COPY --from=frontend-build /app/dist /usr/share/nginx/html
COPY --from=frontend-build /app/package.json /app_frontend/package.json

# 7) rootfs : services S6 (rclone/uvicorn/nginx) + conf nginx + fuse.conf
COPY rootfs/ /

# 8) Filet de sécurité : bits exécutables sur les scripts S6 (si perdus au COPY)
RUN chmod +x /etc/s6-overlay/s6-rc.d/rclone/run \
             /etc/s6-overlay/s6-rc.d/rclone/finish \
             /etc/s6-overlay/s6-rc.d/uvicorn/run \
             /etc/s6-overlay/s6-rc.d/nginx/run

ARG GIT_DESCRIBE=unknown
ARG GIT_BRANCH=unknown
ARG BUILD_DATE=unknown
ARG IMAGE_TAG=unknown
ENV BUILD_GIT_DESCRIBE=$GIT_DESCRIBE BUILD_GIT_BRANCH=$GIT_BRANCH BUILD_DATE=$BUILD_DATE BUILD_IMAGE_TAG=$IMAGE_TAG

EXPOSE 8519
ENTRYPOINT ["/init"]
