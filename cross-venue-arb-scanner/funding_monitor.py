#!/usr/bin/env python3
"""Funding arb 로그 기반 포지션 추적 (DB 없는 상태 대응)."""
import re
import subprocess
from datetime import datetime, timedelta, timezone
from collections import defaultdict


def parse_log_positions():
    """trading.log에서 FUND 진입/청산 매칭."""
    r = subprocess.run(
        ['tail', '-20000', '<INSTALL_DIR>/multi-perp-dex/trading.log'],
        capture_output=True, text=True, timeout=30,
    )
    lines = r.stdout.split('\n')

    open_pat = re.compile(r'(\d\d:\d\d:\d\d).*FUND.*진입 완료.*│ (\w+) Long@(\S+?)\(([-\d.]+)\) Short@(\S+?)\(([-\d.]+)\).*notional=\$([\d.]+) diff=([-\d.]+)%')
    close_pat = re.compile(r'(\d\d:\d\d:\d\d).*FUND.*(청산|close|종료).*(\w+).*long[= ]?(\S+).*short[= ]?(\S+)', re.IGNORECASE)
    stop_pat = re.compile(r'FUND │ 종료 완료')

    positions = []  # in-order
    closed_keys = set()
    restart_idx = 0
    for i, ln in enumerate(lines):
        if stop_pat.search(ln):
            restart_idx = i  # 최근 재시작 마커
    # 최근 재시작 이후 진입만 활성
    for ln in lines[restart_idx:]:
        m = open_pat.search(ln)
        if m:
            ts, coin, long_ex, long_r, short_ex, short_r, notional, diff = m.groups()
            positions.append({
                'ts': ts, 'coin': coin,
                'long_ex': long_ex, 'long_rate': float(long_r),
                'short_ex': short_ex, 'short_rate': float(short_r),
                'notional': float(notional), 'diff_8h_pct': float(diff),
            })
        m2 = close_pat.search(ln)
        if m2:
            coin = m2.group(3)
            # 간단히 coin-level로 close 추적
            for p in positions:
                if p['coin'] == coin and not p.get('closed'):
                    p['closed'] = True
                    break
    return [p for p in positions if not p.get('closed')]


def main():
    pos = parse_log_positions()
    now = datetime.now(timezone.utc)
    next_f = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    mins_left = int((next_f - now).total_seconds() / 60)

    print(f'=== Funding Arb Monitor (log 기반) ===')
    print(f'현재: {now.strftime("%H:%M:%S UTC")}')
    print(f'다음 funding: {next_f.strftime("%H:%M UTC")} ({mins_left}분)')
    print(f'open 포지션: {len(pos)}건')
    print()

    if not pos:
        print('(로그에서 open 포지션 검색 안 됨)')
        return

    total_h = total_8h = 0
    print(f"{'coin':<5} {'long':<18} {'short':<18} {'notional':>9} {'diff/8h':>9} {'est/1h':>9} {'est/8h':>9}")
    for p in pos:
        # 1 funding event ≈ 1h (HL) → diff_8h / 8 per hour
        est_hr = p['notional'] * p['diff_8h_pct'] / 100 / 8
        est_8h = p['notional'] * p['diff_8h_pct'] / 100
        total_h += est_hr
        total_8h += est_8h
        print(f"{p['coin']:<5} {p['long_ex']:<18} {p['short_ex']:<18} ${p['notional']:>6.0f}   {p['diff_8h_pct']:>+6.2f}%  ${est_hr:>+6.3f}  ${est_8h:>+6.3f}")

    print()
    print(f'다음 1h funding 예상 합계: ${total_h:+.3f}')
    print(f'8h (1 funding cycle) 예상: ${total_8h:+.3f}')
    print(f'일일 (24h, 3 cycles 가정): ${total_h*24:+.3f}')


if __name__ == '__main__':
    main()
