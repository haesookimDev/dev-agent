FROM golang:1.24-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY cmd ./cmd
COPY internal ./internal
RUN CGO_ENABLED=0 go build -o /out/kelpie-worker ./cmd/kelpie-worker

FROM alpine:3.22
RUN adduser -D -u 10001 kelpie
USER kelpie
COPY --from=build /out/kelpie-worker /usr/local/bin/kelpie-worker
ENTRYPOINT ["kelpie-worker"]
