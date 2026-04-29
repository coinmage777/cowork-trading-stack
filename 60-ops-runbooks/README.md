# 60-ops-runbooks

봇을 짜는 것과 봇을 운영하는 것은 다른 일입니다. 이 폴더는 Claude Code 로 만든 봇을 실제로 돌릴 때 마주치는 운영 노하우 모음입니다.

대상 독자: AI로 봇은 만들었는데 "이제 이걸 어떻게 24/7 돌리지?"에서 막힌 사람.

## 구성

| # | 파일 | 한 줄 요약 |
|---|------|----------|
| 01 | [tmux-deployment.md](./01-tmux-deployment.md) | Contabo VPS에서 여러 봇을 tmux 세션으로 병렬 운영하는 패턴 |
| 02 | [systemd-service.md](./02-systemd-service.md) | VPS 재부팅 후에도 살아남아야 하는 봇은 systemd unit으로 |
| 03 | [windows-vs-linux.md](./03-windows-vs-linux.md) | Windows 개발기 vs Linux 운영기 — SIGHUP 부재, 인코딩, 경로 지옥 |
| 04 | [git-private-flow.md](./04-git-private-flow.md) | Private repo + AI 워크플로우 — sanitize → commit → push → merge 자동화 |
| 05 | [obsidian-pipeline.md](./05-obsidian-pipeline.md) | Obsidian Vault를 single source of truth로 쓰는 멀티 플랫폼 콘텐츠 파이프라인 |

별도 모듈:

- [telegram-control/](./telegram-control/) — Telegram 봇으로 운영중인 봇을 원격 제어하는 패턴 (별도 문서 세트)

## 핵심 원칙

이 폴더 전체를 관통하는 4가지 원칙:

1. **재시작 가능성 (Restartability)** — 봇은 죽습니다. 죽었을 때 자동으로 다시 살아나고, 살아난 다음 직전 상태를 복구할 수 있어야 합니다. systemd `Restart=on-failure` + `reconcile_positions()` 조합.

2. **관찰 가능성 (Observability)** — `tmux attach`로 실시간 로그를 보거나, `cat status.txt`로 스냅샷을 보거나, `journalctl -u <service> -f`로 tail할 수 있어야 합니다. "봇이 잘 돌고 있는지 모르겠는데 끊었다 켜기 무서움" = 운영 실패.

3. **시크릿 격리 (Secret hygiene)** — `.env`는 절대 커밋하지 않습니다. plaintext private key는 절대 코드에 두지 않습니다. push 전 sanitize grep이 필수 절차입니다.

4. **개발/운영 환경 분리 (Dev/Prod split)** — Windows에서 짜고, Linux에서 돌립니다. 두 환경의 차이 (SIGHUP, 경로 구분자, 인코딩)를 처음부터 인지하고 코드 짜는 것이 나중에 디버깅 시간을 아낍니다.

## 읽는 순서

처음 읽는다면 01 → 02 → 03 순서를 권장합니다. 04, 05는 독립적입니다.

이미 봇을 돌리고 있다면 03번 (Windows vs Linux 차이)부터 보는 것을 추천합니다 — 가장 많이 부딪히는 함정이 모여 있습니다.
