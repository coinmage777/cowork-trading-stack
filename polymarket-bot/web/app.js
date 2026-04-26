const money = (value) => new Intl.NumberFormat('ko-KR', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 }).format(value || 0);
const pct = (value) => `${((value || 0) * 100).toFixed(1)}%`;
const shortDate = (value) => value ? value.replace('T', ' ').slice(0, 16) : '-';
const strategyPalette = ['#72f0ff', '#5ff2a2', '#ff83cc'];

function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}

function parseOptimizerNotes(notes) {
  if (!notes) return {};
  const riskMatch = notes.match(/risk_scale=([0-9.]+)/);
  const blockedMatch = notes.match(/blocked=([^\s,]+(?:,[^\s,]+)*)/);
  const blocked = blockedMatch && blockedMatch[1] && blockedMatch[1] !== '-'
    ? blockedMatch[1].split(',').map((name) => name.trim()).filter(Boolean)
    : [];
  return {
    risk_scale: riskMatch ? Number(riskMatch[1]) : undefined,
    blocked_strategies: blocked,
  };
}

function buildHourlyFromRecentTrades(rows) {
  if (!rows.length) return { hourly_rows: [], hourly_change: {} };
  const buckets = new Map();
  rows.forEach((trade) => {
    const ts = trade.timestamp ? new Date(trade.timestamp) : null;
    if (!ts || Number.isNaN(ts.getTime())) return;
    const hour = ts.getHours();
    const key = `${hour}`.padStart(2, '0');
    if (!buckets.has(key)) {
      buckets.set(key, { hour, label: `${key}:00`, pnl: 0, wins: 0, trade_count: 0 });
    }
    const item = buckets.get(key);
    const pnl = Number(trade.pnl || 0);
    item.pnl += pnl;
    item.trade_count += 1;
    if (pnl > 0) item.wins += 1;
  });

  const hourlyRows = [...buckets.values()]
    .sort((a, b) => a.hour - b.hour)
    .map((row) => ({
      label: row.label,
      pnl: row.pnl,
      trade_count: row.trade_count,
      win_rate: row.trade_count ? row.wins / row.trade_count : 0,
    }));

  const byEpochHour = new Map();
  rows.forEach((trade) => {
    const ts = trade.timestamp ? new Date(trade.timestamp) : null;
    if (!ts || Number.isNaN(ts.getTime())) return;
    const epochHour = Math.floor(ts.getTime() / 3600000);
    if (!byEpochHour.has(epochHour)) {
      byEpochHour.set(epochHour, { pnl: 0, wins: 0, trade_count: 0 });
    }
    const item = byEpochHour.get(epochHour);
    const pnl = Number(trade.pnl || 0);
    item.pnl += pnl;
    item.trade_count += 1;
    if (pnl > 0) item.wins += 1;
  });

  const sortedEpoch = [...byEpochHour.keys()].sort((a, b) => b - a);
  const last = sortedEpoch[0];
  const prev = sortedEpoch[1];
  const lastHour = last != null ? byEpochHour.get(last) : null;
  const prevHour = prev != null ? byEpochHour.get(prev) : null;

  const lastDate = last != null ? new Date(last * 3600000) : null;
  const prevDate = prev != null ? new Date(prev * 3600000) : null;
  const toLabel = (d) => (d ? `${`${d.getHours()}`.padStart(2, '0')}:00` : '-');
  const fmt = (item) => item ? ({
    pnl: item.pnl,
    trade_count: item.trade_count,
    win_rate: item.trade_count ? item.wins / item.trade_count : 0,
  }) : { pnl: 0, trade_count: 0, win_rate: 0 };

  const l = fmt(lastHour);
  const p = fmt(prevHour);
  return {
    hourly_rows: hourlyRows,
    hourly_change: {
      last_hour_label: toLabel(lastDate),
      prev_hour_label: toLabel(prevDate),
      last_hour: l,
      prev_hour: p,
      delta_pnl: l.pnl - p.pnl,
      delta_win_rate: l.win_rate - p.win_rate,
      delta_trades: l.trade_count - p.trade_count,
    },
  };
}

function normalizePayload(payload) {
  const safe = { ...payload };
  safe.metrics = safe.metrics || {};
  safe.paper_scoreboard = safe.paper_scoreboard || {};
  safe.headline = safe.headline || {};

  if (!safe.paper_scoreboard.today_pnl) safe.paper_scoreboard.today_pnl = Number(safe.headline.paper_today_pnl || 0);
  if (!safe.paper_scoreboard.today_win_rate) safe.paper_scoreboard.today_win_rate = Number(safe.headline.paper_today_win_rate || 0);
  if (!safe.paper_scoreboard.recent20_pnl) safe.paper_scoreboard.recent20_pnl = Number(safe.headline.recent20_pnl || 0);
  if (!safe.paper_scoreboard.goal_hit) safe.paper_scoreboard.goal_hit = Boolean(safe.headline.paper_goal_hit);
  if (safe.headline.live_ready == null) safe.headline.live_ready = false;

  if (!safe.paper_gate) {
    safe.paper_gate = {
      ready: false,
      sample_size: Number(safe.headline.paper_today_trades || safe.paper_scoreboard.today_trades || 0),
      sample_pnl: Number(safe.headline.recent20_pnl || safe.paper_scoreboard.recent20_pnl || 0),
      sample_win_rate: Number(safe.headline.recent20_win_rate || safe.paper_scoreboard.today_win_rate || 0),
      required_trades: null,
      required_pnl: null,
      required_win_rate: null,
      remaining_trades: null,
      remaining_pnl: null,
      remaining_win_rate: null,
      message: '게이트 기준값이 백엔드에서 제공되지 않아 샘플 요약만 표시합니다.',
    };
  }

  const optimizer = safe.optimizer || {};
  const latestEvent = (optimizer.events || [])[0] || {};
  const parsed = parseOptimizerNotes(latestEvent.notes || '');
  if (optimizer.regime == null) optimizer.regime = safe.paper_optimizer_state?.last_regime || '-';
  if (optimizer.risk_scale == null) optimizer.risk_scale = parsed.risk_scale ?? 1.0;
  if (!Array.isArray(optimizer.blocked_strategies) || !optimizer.blocked_strategies.length) {
    optimizer.blocked_strategies = parsed.blocked_strategies || [];
  }
  if (optimizer.sample_pnl == null) optimizer.sample_pnl = Number(safe.headline.recent20_pnl || 0);
  if (optimizer.sample_size == null) optimizer.sample_size = Number(safe.headline.paper_today_trades || 0);
  safe.optimizer = optimizer;

  const recentPaper = safe.metrics.recent_paper || [];
  if (!safe.metrics.hourly_rows || !safe.metrics.hourly_rows.length || !safe.metrics.hourly_change) {
    const hourly = buildHourlyFromRecentTrades(recentPaper);
    if (!safe.metrics.hourly_rows || !safe.metrics.hourly_rows.length) safe.metrics.hourly_rows = hourly.hourly_rows;
    if (!safe.metrics.hourly_change) safe.metrics.hourly_change = hourly.hourly_change;
  }

  if (!safe.recommendation) {
    const recoveryLocked = Boolean(safe.recovery && safe.recovery.locked);
    safe.recommendation = {
      confidence: recoveryLocked ? 'high' : 'medium',
      headline: recoveryLocked ? '복구 잠금 유지' : '페이퍼 모드 유지',
      summary: recoveryLocked
        ? '복구 목표 미달로 실전 전환을 계속 차단합니다.'
        : '실전 전환 전 추가 페이퍼 표본을 누적하세요.',
      actions: [
        '실전 주문은 비활성 상태를 유지합니다.',
        '시간대별 손익과 승률 변화가 안정화되는지 관찰합니다.',
      ],
    };
  }

  return safe;
}

function renderLineChart(svgId, points, key, color, fillColor) {
  const svg = document.getElementById(svgId);
  if (!svg) return;
  const width = 900;
  const height = 320;
  const pad = 28;
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  if (!points.length) {
    svg.innerHTML = `<text x="24" y="48" fill="#8ea7c7" font-size="15">데이터가 아직 충분하지 않습니다.</text>`;
    return;
  }

  const values = points.map((point) => Number(point[key] || 0));
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const stepX = (width - pad * 2) / Math.max(points.length - 1, 1);
  const toY = (value) => height - pad - ((value - min) / range) * (height - pad * 2);
  const linePath = points.map((point, index) => `${index === 0 ? 'M' : 'L'} ${pad + index * stepX} ${toY(Number(point[key] || 0))}`).join(' ');
  const areaPath = `${linePath} L ${pad + (points.length - 1) * stepX} ${height - pad} L ${pad} ${height - pad} Z`;

  const guides = Array.from({ length: 4 }, (_, idx) => {
    const y = pad + ((height - pad * 2) / 3) * idx;
    return `<line x1="${pad}" y1="${y}" x2="${width - pad}" y2="${y}" stroke="rgba(255,255,255,0.08)" stroke-dasharray="4 6" />`;
  }).join('');

  svg.innerHTML = `
    <defs>
      <linearGradient id="${svgId}-fill" x1="0" x2="0" y1="0" y2="1">
        <stop offset="0%" stop-color="${fillColor}" stop-opacity="0.35" />
        <stop offset="100%" stop-color="${fillColor}" stop-opacity="0" />
      </linearGradient>
    </defs>
    ${guides}
    <path d="${areaPath}" fill="url(#${svgId}-fill)"></path>
    <path d="${linePath}" fill="none" stroke="${color}" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"></path>
  `;
}

function renderMultiLineChart(svgId, series) {
  const svg = document.getElementById(svgId);
  if (!svg) return;
  const width = 900;
  const height = 320;
  const pad = 28;
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  if (!series.length) {
    svg.innerHTML = `<text x="24" y="48" fill="#8ea7c7" font-size="15">전략별 곡선을 그릴 만큼 표본이 아직 적습니다.</text>`;
    return;
  }
  const allValues = series.flatMap((item) => item.points.map((point) => Number(point.equity || 0)));
  const allX = series.flatMap((item) => item.points.map((point) => Number(point.x || 0)));
  const min = Math.min(...allValues);
  const max = Math.max(...allValues);
  const range = max - min || 1;
  const minX = Math.min(...allX);
  const maxX = Math.max(...allX);
  const xRange = maxX - minX || 1;
  const toY = (value) => height - pad - ((value - min) / range) * (height - pad * 2);
  const toX = (value) => pad + ((value - minX) / xRange) * (width - pad * 2);

  const guides = Array.from({ length: 4 }, (_, idx) => {
    const y = pad + ((height - pad * 2) / 3) * idx;
    return `<line x1="${pad}" y1="${y}" x2="${width - pad}" y2="${y}" stroke="rgba(255,255,255,0.08)" stroke-dasharray="4 6" />`;
  }).join('');

  const lines = series.map((item, idx) => {
    const color = strategyPalette[idx % strategyPalette.length];
    const d = item.points.map((point, pointIdx) => `${pointIdx === 0 ? 'M' : 'L'} ${toX(Number(point.x || 0))} ${toY(Number(point.equity || 0))}`).join(' ');
    return `<path d="${d}" fill="none" stroke="${color}" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"></path>`;
  }).join('');

  svg.innerHTML = `${guides}${lines}`;
  const legend = document.getElementById('strategy-legend');
  if (legend) {
    legend.innerHTML = series.map((item, idx) => `<span class="legend-item"><i style="background:${strategyPalette[idx % strategyPalette.length]}"></i>${item.strategy}</span>`).join('');
  }
}

function renderBarChart(svgId, rows, valueKey, positiveColor, negativeColor) {
  const svg = document.getElementById(svgId);
  if (!svg) return;
  const width = svgId === 'histogram-chart' ? 420 : 900;
  const height = 250;
  const pad = 28;
  if (!rows.length) {
    svg.innerHTML = `<text x="24" y="48" fill="#8ea7c7" font-size="15">표시할 데이터가 없습니다.</text>`;
    return;
  }
  const values = rows.map((row) => Number(row[valueKey] || 0));
  const max = Math.max(...values, 0);
  const min = Math.min(...values, 0);
  const range = max - min || 1;
  const zeroY = height - pad - ((0 - min) / range) * (height - pad * 2);
  const barWidth = (width - pad * 2) / rows.length - 8;
  const bars = rows.map((row, index) => {
    const value = Number(row[valueKey] || 0);
    const x = pad + index * ((width - pad * 2) / rows.length) + 4;
    const y = value >= 0 ? height - pad - ((value - min) / range) * (height - pad * 2) : zeroY;
    const h = Math.abs((value / range) * (height - pad * 2));
    const color = value >= 0 ? positiveColor : negativeColor;
    const label = row.date || row.label || row.mode || '';
    return `
      <rect x="${x}" y="${Math.min(y, zeroY)}" width="${Math.max(barWidth, 10)}" height="${Math.max(h, 2)}" rx="8" fill="${color}"></rect>
      <text x="${x + barWidth / 2}" y="${height - 8}" fill="#8ea7c7" font-size="11" text-anchor="middle">${label.slice(0, 5)}</text>
    `;
  }).join('');
  svg.innerHTML = `
    <line x1="${pad}" y1="${zeroY}" x2="${width - pad}" y2="${zeroY}" stroke="rgba(255,255,255,0.15)" />
    ${bars}
  `;
}

function fillRecovery(recovery) {
  if (!recovery) return;
  const ratio = recovery.progress || 0;
  document.getElementById('recovery-fill').style.width = `${ratio * 100}%`;
  setText('recovery-progress', `${(ratio * 100).toFixed(1)}% 진행`);
  setText('recovery-effective', `유효 손익 ${money(recovery.effective_pnl)}`);
  const detail = document.getElementById('recovery-detail');
  detail.innerHTML = [
    ['시작 손익', money(recovery.start_pnl)],
    ['오늘 전체 손익', money(recovery.today_total_pnl)],
    ['복구 목표', money(recovery.target_pnl)],
    ['남은 금액', money(recovery.remaining)],
  ].map(([label, value]) => `<div><dt>${label}</dt><dd>${value}</dd></div>`).join('');
}

function fillGate(gate, recoveryLocked) {
  if (!gate) return;
  const status = document.getElementById('live-gate-status');
  const req = document.getElementById('live-gate-requirements');
  const dash = '-';
  status.innerHTML = `
    <div class="gate-pill ${gate.ready && !recoveryLocked ? 'gate-ready' : 'gate-blocked'}">${gate.ready && !recoveryLocked ? '실전 후보 가능' : '실전 잠금 유지'}</div>
    <strong>${gate.message}</strong>
    <p>${gate.reference_group || 'reference'} 최근 ${gate.sample_size}건 기준 손익 ${money(gate.sample_pnl)}, 승률 ${pct(gate.sample_win_rate)}</p>
  `;
  req.innerHTML = [
    ['남은 거래 수', gate.remaining_trades == null ? dash : `${gate.remaining_trades}건`],
    ['남은 손익', gate.remaining_pnl == null ? dash : money(gate.remaining_pnl)],
    ['남은 승률 갭', gate.remaining_win_rate == null ? dash : pct(gate.remaining_win_rate)],
    ['기준치', gate.required_trades == null ? dash : `${gate.required_trades}건 / ${money(gate.required_pnl)} / ${pct(gate.required_win_rate)}`],
  ].map(([label, value]) => `<div class="mini-stat"><span>${label}</span><strong>${value}</strong></div>`).join('');
}

function fillRecommendation(recommendation) {
  if (!recommendation) return;
  const confidenceMap = { low: '신뢰도 낮음', medium: '신뢰도 중간', high: '신뢰도 높음' };
  setText('recommendation-confidence', confidenceMap[recommendation.confidence] || '신뢰도 낮음');
  setText('recommendation-headline', recommendation.headline || '-');
  setText('recommendation-summary', recommendation.summary || '-');
  document.getElementById('recommendation-actions').innerHTML = (recommendation.actions || []).map((item) => `
    <div class="action-item"><span class="action-dot"></span><p>${item}</p></div>
  `).join('');
}

function fillOptimizer(optimizer) {
  if (!optimizer) return;
  setText('optimizer-phase', `${optimizer.phase || '-'} / ${optimizer.regime || '-'}`);
  setText('optimizer-profile', optimizer.profile_name || '-');
  setText('optimizer-risk', `${Number(optimizer.risk_scale || 0).toFixed(2)}x`);
  setText('optimizer-sample', `${money(optimizer.sample_pnl || 0)} / ${optimizer.sample_size || 0}건`);
  setText('optimizer-message', optimizer.message || '-');
  const config = optimizer.active_config || {};
  document.getElementById('optimizer-config').innerHTML = `
    <h3>현재 적용 파라미터</h3>
    <div class="stat-row"><span>최소 엣지</span><strong>${Number(config.min_edge_threshold || 0).toFixed(3)}</strong></div>
    <div class="stat-row"><span>최소 진입 엣지</span><strong>${Number(config.min_entry_edge || 0).toFixed(3)}</strong></div>
    <div class="stat-row"><span>켈리 비율</span><strong>${Number(config.kelly_fraction || 0).toFixed(3)}</strong></div>
    <div class="stat-row"><span>최대 베팅</span><strong>${money(config.max_single_bet || 0)}</strong></div>
    <div class="stat-row"><span>최소 유동성</span><strong>${money(config.min_market_liquidity || 0)}</strong></div>
    <div class="stat-row"><span>최소 만기 여유</span><strong>${Number(config.min_time_to_expiry || 0).toFixed(1)}분</strong></div>
  `;
  document.getElementById('blocked-strategies').innerHTML = (optimizer.blocked_strategies || []).length
    ? optimizer.blocked_strategies.map((name) => `<span class="chip chip-bad">${name}</span>`).join('')
    : '<span class="chip chip-good">현재 자동 차단된 전략 없음</span>';
  document.getElementById('optimizer-history').innerHTML = (optimizer.events || []).map((event) => `
    <article class="timeline-item">
      <strong>${event.profile_name} | 샘플 ${money(event.sample_pnl)} | 승률 ${pct(event.sample_win_rate)}</strong>
      <p>${shortDate(event.timestamp)} · ${event.notes || '프로필 전환 기록'}</p>
    </article>
  `).join('');
}

function fillHourlySummary(rows, topRows, bottomRows) {
  const fallback = (rows || []).filter((row) => Number(row.trade_count || 0) >= 2);
  const best = (topRows && topRows.length) ? topRows[0] : [...fallback].sort((a, b) => Number(b.pnl || 0) - Number(a.pnl || 0))[0];
  const worst = (bottomRows && bottomRows.length) ? bottomRows[0] : [...fallback].sort((a, b) => Number(a.pnl || 0) - Number(b.pnl || 0))[0];
  if (!best || !worst) {
    document.getElementById('hourly-summary').innerHTML = '<div class="chip chip-good">시간대별 해석을 할 만큼 표본이 아직 적습니다.</div>';
    return;
  }
  document.getElementById('hourly-summary').innerHTML = `
    <div class="mini-card">
      <span>가장 좋은 시간대</span>
      <strong>${best.label}</strong>
      <p>${money(best.pnl)} / 승률 ${pct(best.win_rate)} / ${best.trade_count}건</p>
    </div>
    <div class="mini-card">
      <span>가장 약한 시간대</span>
      <strong>${worst.label}</strong>
      <p>${money(worst.pnl)} / 승률 ${pct(worst.win_rate)} / ${worst.trade_count}건</p>
    </div>
  `;
}

function rankText(rank) {
  return rank == null ? '-' : `#${rank}`;
}

function fillDriftPanels(metrics) {
  const changesEl = document.getElementById('strategy-rank-changes');
  const deltaEl = document.getElementById('hourly-delta');
  if (!changesEl || !deltaEl) return;

  const changes = metrics.strategy_rank_changes || [];
  if (!changes.length) {
    changesEl.innerHTML = '<div class="chip chip-good">최근 40건 기준 전략 순위 변동 없음</div>';
  } else {
    changesEl.innerHTML = changes.slice(0, 8).map((row) => `
      <article class="timeline-item">
        <strong>${row.strategy_name} ${rankText(row.previous_rank)} → ${rankText(row.latest_rank)}</strong>
        <p>최근20 ${money(row.latest_window_pnl)} | 이전20 ${money(row.previous_window_pnl)}</p>
      </article>
    `).join('');
  }

  const hc = metrics.hourly_change || {};
  const last = hc.last_hour || {};
  const prev = hc.prev_hour || {};
  const pnlCls = Number(hc.delta_pnl || 0) >= 0 ? 'pnl-pos' : 'pnl-neg';
  const wrCls = Number(hc.delta_win_rate || 0) >= 0 ? 'pnl-pos' : 'pnl-neg';
  deltaEl.innerHTML = `
    <div class="mini-stat"><span>${hc.last_hour_label || '최근 1시간'}</span><strong>${money(last.pnl || 0)} / ${pct(last.win_rate || 0)} / ${last.trade_count || 0}건</strong></div>
    <div class="mini-stat"><span>${hc.prev_hour_label || '이전 1시간'}</span><strong>${money(prev.pnl || 0)} / ${pct(prev.win_rate || 0)} / ${prev.trade_count || 0}건</strong></div>
    <div class="mini-stat"><span>손익 변화</span><strong class="${pnlCls}">${money(hc.delta_pnl || 0)}</strong></div>
    <div class="mini-stat"><span>승률 변화</span><strong class="${wrCls}">${pct(hc.delta_win_rate || 0)}</strong></div>
    <div class="mini-stat"><span>거래 수 변화</span><strong>${Number(hc.delta_trades || 0) > 0 ? '+' : ''}${hc.delta_trades || 0}건</strong></div>
  `;
}

function fillTables(payload) {
  const paperRows = payload.metrics.recent_paper || [];
  document.getElementById('paper-history-body').innerHTML = paperRows.map((trade) => {
    const pnl = Number(trade.pnl || 0);
    const pnlClass = pnl > 0 ? 'pnl-pos' : pnl < 0 ? 'pnl-neg' : 'pnl-flat';
    return `
      <tr>
        <td>${shortDate(trade.timestamp)}</td>
        <td>${(trade.market_question || '').slice(0, 42)}</td>
        <td>${trade.strategy_name || '-'}</td>
        <td>${trade.side || '-'}</td>
        <td>${Number(trade.entry_price || 0).toFixed(4)}</td>
        <td>${trade.exit_price == null ? '-' : Number(trade.exit_price).toFixed(4)}</td>
        <td class="${pnlClass}">${money(pnl)}</td>
      </tr>
    `;
  }).join('');

  const openRows = payload.metrics.open_positions || [];
  document.getElementById('open-positions-body').innerHTML = openRows.map((trade) => `
    <tr>
      <td>${(trade.market_question || '').slice(0, 42)}</td>
      <td>${trade.mode || '-'}</td>
      <td>${trade.side || '-'}</td>
      <td>${money(trade.size || 0)}</td>
      <td>${Number(trade.entry_price || 0).toFixed(4)}</td>
      <td>${trade.expiry_time ? new Date(Number(trade.expiry_time) * 1000).toLocaleString('ko-KR') : '-'}</td>
    </tr>
  `).join('');

  document.getElementById('mode-breakdown').innerHTML = (payload.metrics.mode_breakdown || []).map((row) => `
    <div class="mode-row">
      <div>
        <strong>${row.mode}</strong>
        <div class="meta">종결 거래 ${row.count}건</div>
      </div>
      <strong class="${Number(row.pnl || 0) >= 0 ? 'pnl-pos' : 'pnl-neg'}">${money(row.pnl || 0)}</strong>
    </div>
  `).join('');

  document.getElementById('strategy-score-body').innerHTML = (payload.metrics.strategy_rows || []).map((row) => `
    <tr>
      <td>${row.strategy_name || '-'}</td>
      <td>${row.trade_count || 0}</td>
      <td>${pct(row.win_rate || 0)}</td>
      <td class="${Number(row.avg_pnl || 0) >= 0 ? 'pnl-pos' : 'pnl-neg'}">${money(row.avg_pnl || 0)}</td>
      <td class="${Number(row.total_pnl || 0) >= 0 ? 'pnl-pos' : 'pnl-neg'}">${money(row.total_pnl || 0)}</td>
    </tr>
  `).join('');

  document.getElementById('profile-score-body').innerHTML = (payload.metrics.profile_rows || []).map((row) => `
    <tr>
      <td>${row.profile_name || '-'}</td>
      <td>${row.trade_count || 0}</td>
      <td>${pct(row.win_rate || 0)}</td>
      <td class="${Number(row.avg_pnl || 0) >= 0 ? 'pnl-pos' : 'pnl-neg'}">${money(row.avg_pnl || 0)}</td>
      <td class="${Number(row.total_pnl || 0) >= 0 ? 'pnl-pos' : 'pnl-neg'}">${money(row.total_pnl || 0)}</td>
    </tr>
  `).join('');

  document.getElementById('group-score-body').innerHTML = (payload.metrics.group_rows || []).map((row) => `
    <tr>
      <td>${row.market_group || '-'}</td>
      <td>${row.asset_symbol || '-'}</td>
      <td>${row.trade_count || 0}</td>
      <td>${pct(row.win_rate || 0)}</td>
      <td class="${Number(row.avg_pnl || 0) >= 0 ? 'pnl-pos' : 'pnl-neg'}">${money(row.avg_pnl || 0)}</td>
      <td class="${Number(row.total_pnl || 0) >= 0 ? 'pnl-pos' : 'pnl-neg'}">${money(row.total_pnl || 0)}</td>
    </tr>
  `).join('');
}

function fillHeader(payload) {
  const goalHit = payload.paper_scoreboard.goal_hit;
  const liveReady = payload.headline.live_ready;
  const pill = document.getElementById('goal-pill');
  pill.textContent = liveReady ? '실전 후보 가능' : goalHit ? '목표 달성' : (payload.recovery.locked ? '복구 진행 중' : '수익 확인 중');
  pill.className = `pill ${liveReady ? 'goal-hit' : (goalHit ? 'goal-wait' : (payload.recovery.locked ? 'goal-risk' : 'goal-wait'))}`;
  setText('generated-at', `업데이트: ${new Date(payload.generated_at).toLocaleString('ko-KR')}`);
  setText('paper-today-pnl', money(payload.paper_scoreboard.today_pnl));
  setText('paper-win-rate', pct(payload.paper_scoreboard.today_win_rate));
  setText('recent20-pnl', money(payload.paper_scoreboard.recent20_pnl));
  setText('recovery-remaining', money(payload.recovery.remaining));
  setText('paper-goal-copy', liveReady ? '최근 페이퍼 표본은 실전 게이트를 통과했습니다.' : '아직은 검증 구간입니다. 플러스 손익과 충분한 거래 수를 채워야 합니다.');
}

function fillTranslations(translations) {
  setText('translation-paper-goal', translations.paper_goal || '');
  setText('translation-profit-notice', translations.profit_notice || '');
  setText('translation-optimizer-note', translations.optimizer_note || '');
}

async function refresh() {
  const res = await fetch('./api/dashboard', { cache: 'no-store' });
  const payload = normalizePayload(await res.json());
  fillHeader(payload);
  fillRecovery(payload.recovery);
  fillGate(payload.paper_gate, payload.recovery.locked);
  fillRecommendation(payload.recommendation);
  fillOptimizer(payload.optimizer);
  fillTables(payload);
  fillHourlySummary(payload.metrics.hourly_rows || [], payload.metrics.hourly_top_active || [], payload.metrics.hourly_bottom_active || []);
  fillDriftPanels(payload.metrics || {});
  fillTranslations(payload.translations || {});
  renderLineChart('equity-chart', payload.metrics.equity_curve || [], 'equity', '#72f0ff', '#72f0ff');
  renderMultiLineChart('strategy-chart', payload.metrics.strategy_curves || []);
  renderBarChart('daily-chart', payload.metrics.daily_rows || [], 'realized_pnl', '#5ff2a2', '#ff7f88');
  renderBarChart('hourly-chart', payload.metrics.hourly_rows || [], 'pnl', '#72f0ff', '#ff7f88');
  renderBarChart('histogram-chart', payload.metrics.pnl_histogram || [], 'count', '#f8c66d', '#f8c66d');
}

refresh();
setInterval(refresh, 5000);




