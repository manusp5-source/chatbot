# Build
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json* ./
# npm ci: instala EXACTAMENTE el lockfile (builds reproducibles).
RUN npm ci
COPY . .
# URL de la API, que Vite hornea en el build del panel. NO tiene valor por
# defecto a propósito: apuntar al backend de otra instalación sería peor que
# fallar. Pásala al construir:
#   --build-arg VITE_API_BASE_URL=https://api.tudominio.com
# o en EasyPanel: servicio frontend → Build → Build Args.
ARG VITE_API_BASE_URL
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN test -n "$VITE_API_BASE_URL" || (echo "ERROR: VITE_API_BASE_URL no definido. Pasalo con --build-arg." && exit 1)
RUN npm run build

# Serve
FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
# Override en RUNTIME de la URL del backend: la imagen oficial de nginx ejecuta
# los scripts de /docker-entrypoint.d/ antes de arrancar, y este genera
# /config.js a partir de API_BASE_URL. Si API_BASE_URL no se define, config.js
# queda vacío y el frontend usa el default horneado arriba (VITE_API_BASE_URL),
# así el despliegue actual no cambia.
COPY docker-entrypoint.d/40-app-config.sh /docker-entrypoint.d/40-app-config.sh
RUN chmod +x /docker-entrypoint.d/40-app-config.sh
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
