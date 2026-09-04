FROM golang:1.24-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY . .
RUN CGO_ENABLED=0 go build -o /out/kelpie-gateway .

FROM alpine:3.22
RUN adduser -D -u 10001 kelpie
USER kelpie
COPY --from=build /out/kelpie-gateway /usr/local/bin/kelpie-gateway
ENTRYPOINT ["kelpie-gateway"]
