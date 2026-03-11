// ── API ─────────────────────────────────────────
const API = {
    me: () => fetch('/api/me').then(r => r.json()),
    policies: () => fetch('/api/policies').then(r => r.ok ? r.json() : null),
    refresh: () => fetch('/api/policies/refresh', { method: 'POST' }).then(r => {
        if (!r.ok) return r.json().then(e => { throw new Error(e.error || 'Refresh failed'); });
        return r.json();
    }),
};

// ── State ───────────────────────────────────────
let isRefreshing = false;
let allPolicies = [];
let currentFilter = 'all';
let hiddenPolicies = new Set();
let currentUserEmail = null;

// ── Init ────────────────────────────────────────
async function init() {
    try {
        const user = await API.me();
        if (user.authenticated) {
            currentUserEmail = user.email;
            loadHiddenPolicies();
            showMainScreen(user);
            await loadPolicies();
        } else {
            showLoginScreen();
        }
    } catch {
        showLoginScreen();
    }
}

function showLoginScreen() {
    document.getElementById('login-screen').classList.remove('hidden');
    document.getElementById('main-screen').classList.add('hidden');
}

function showMainScreen(user) {
    document.getElementById('login-screen').classList.add('hidden');
    document.getElementById('main-screen').classList.remove('hidden');
    document.getElementById('user-name').textContent = user.name || user.email;
}

// ── Load Policies ───────────────────────────────
async function loadPolicies() {
    const data = await API.policies();
    if (data && data.policies && data.policies.length > 0) {
        allPolicies = data.policies.filter(p => p.policy_number || p.policy_end);
        const visible = allPolicies.filter(p => !hiddenPolicies.has(p.policy_number));
        renderSummary(visible);
        renderFiltered();
        updateCacheInfo(data.fetched_at, data.from_cache);
        document.getElementById('empty-state').classList.add('hidden');
        document.getElementById('filter-bar').classList.remove('hidden');
    } else {
        document.getElementById('empty-state').classList.remove('hidden');
        document.getElementById('policies-container').innerHTML = '';
        document.getElementById('summary-bar').classList.add('hidden');
        document.getElementById('filter-bar').classList.add('hidden');
    }
}

// ── Hidden Policies ─────────────────────────────
function loadHiddenPolicies() {
    try {
        const key = 'hidden_policies_' + (currentUserEmail || 'default');
        const stored = localStorage.getItem(key);
        hiddenPolicies = stored ? new Set(JSON.parse(stored)) : new Set();
    } catch { hiddenPolicies = new Set(); }
}

function saveHiddenPolicies() {
    const key = 'hidden_policies_' + (currentUserEmail || 'default');
    localStorage.setItem(key, JSON.stringify([...hiddenPolicies]));
}

function toggleHidePolicy(index, event) {
    event.stopPropagation();
    const filtered = getFilteredPolicies();
    const p = filtered[index];
    if (!p || !p.policy_number) return;

    if (currentFilter === 'hidden') {
        hiddenPolicies.delete(p.policy_number);
    } else {
        hiddenPolicies.add(p.policy_number);
    }
    saveHiddenPolicies();
    const visiblePolicies = allPolicies.filter(p => !hiddenPolicies.has(p.policy_number));
    renderSummary(visiblePolicies);
    renderFiltered();
    updateHiddenChip();
}

function updateHiddenChip() {
    const bar = document.getElementById('filter-bar');
    let chip = document.getElementById('hidden-chip');
    const count = hiddenPolicies.size;

    if (count === 0) {
        if (chip) chip.remove();
        if (currentFilter === 'hidden') setFilter('all');
        return;
    }

    if (!chip) {
        chip = document.createElement('button');
        chip.id = 'hidden-chip';
        chip.className = 'filter-chip';
        chip.dataset.filter = 'hidden';
        chip.onclick = () => setFilter('hidden');
        bar.appendChild(chip);
    }
    chip.textContent = `Hidden (${count})`;
    chip.classList.toggle('active', currentFilter === 'hidden');
}

// ── Filters ─────────────────────────────────────
function setFilter(filter) {
    currentFilter = filter;
    document.querySelectorAll('.filter-chip').forEach(el => {
        el.classList.toggle('active', el.dataset.filter === filter);
    });
    renderFiltered();
}

function renderFiltered() {
    const filtered = allPolicies.filter(p => {
        const isHidden = hiddenPolicies.has(p.policy_number);
        if (currentFilter === 'hidden') return isHidden;
        if (isHidden) return false;

        const status = (p.status || '').toUpperCase();
        const days = p.policy_end ? daysUntil(p.policy_end) : null;

        switch (currentFilter) {
            case 'active':
                return status === 'ACTIVE';
            case 'expiring':
                return days !== null && days > 0 && days <= 90;
            case 'inactive':
                return status === 'EXPIRED' || (days !== null && days <= 0);
            default:
                return true;
        }
    });
    renderPolicies(filtered);
    updateHiddenChip();
}

// ── Refresh (SSE) ──────────────────────────────
function refreshPolicies(forceRefresh = false) {
    if (isRefreshing) return;

    const vaultKey = prompt('Enter vault key:', 'Ashish');
    if (vaultKey === null) return; // cancelled

    isRefreshing = true;
    const _refreshStart = Date.now();

    const refreshBtn = document.getElementById('refresh-btn');
    refreshBtn.disabled = true;
    showProgress(forceRefresh ? 'Force re-extracting all policies...' : 'Searching Gmail for insurance emails...');
    setActiveStage('gmail');

    let url = '/api/policies/refresh-stream?vault_key=' + encodeURIComponent(vaultKey);
    if (forceRefresh) url += '&force=true';
    const es = new EventSource(url);

    es.addEventListener('progress', (e) => {
        const d = JSON.parse(e.data);
        const elapsed = ((Date.now() - _refreshStart) / 1000).toFixed(0);
        const tsPrefix = d.ts ? `[${d.ts}] ` : '';
        updateProgress(d.pct, `${tsPrefix}${d.message} (${elapsed}s)`);
        if (d.stage) setActiveStage(d.stage);
    });

    es.addEventListener('stage_complete', (e) => {
        const d = JSON.parse(e.data);
        const elapsed = ((Date.now() - _refreshStart) / 1000).toFixed(0);
        const tsPrefix = d.ts ? `[${d.ts}] ` : '';
        updateProgress(d.pct || null, `${tsPrefix}${d.message} (${elapsed}s)`);
        if (d.stage) completeStage(d.stage);
    });

    es.addEventListener('done', (e) => {
        const d = JSON.parse(e.data);
        es.close();
        completeStage('finalize');
        const totalElapsed = d.elapsed || ((Date.now() - _refreshStart) / 1000).toFixed(1);
        updateProgress(100, `Done in ${totalElapsed}s!`);

        setTimeout(() => {
            hideProgress();
            const complete = (d.policies || []).filter(p => p.policy_number || p.policy_end);
            if (complete.length > 0) {
                allPolicies = complete;
                const visible = allPolicies.filter(p => !hiddenPolicies.has(p.policy_number));
                renderSummary(visible);
                renderFiltered();
                updateCacheInfo(d.fetched_at, false);
                document.getElementById('empty-state').classList.add('hidden');
                document.getElementById('filter-bar').classList.remove('hidden');
            } else {
                document.getElementById('empty-state').classList.remove('hidden');
                document.getElementById('policies-container').innerHTML = '';
                document.getElementById('summary-bar').classList.add('hidden');
                document.getElementById('filter-bar').classList.add('hidden');
            }
            isRefreshing = false;
            refreshBtn.disabled = false;
        }, 400);
    });

    es.addEventListener('error_event', (e) => {
        const d = JSON.parse(e.data);
        es.close();
        hideProgress();
        showToast('Refresh failed: ' + d.message);
        isRefreshing = false;
        refreshBtn.disabled = false;
        if (d.message && (d.message.includes('re-authenticate') || d.message.includes('No credentials'))) {
            refreshBtn.textContent = 'Re-login';
            refreshBtn.onclick = () => { window.location.href = '/auth/login'; };
        }
    });

    es.onerror = () => {
        es.close();
        hideProgress();
        showToast('Connection lost during refresh.');
        isRefreshing = false;
        refreshBtn.disabled = false;
    };
}

// ── Progress ────────────────────────────────────
function showProgress(text) {
    const section = document.getElementById('progress-section');
    section.classList.remove('hidden');
    document.getElementById('progress-fill').style.width = '2%';
    document.getElementById('progress-text').textContent = text;
    // Reset all stage dots
    document.querySelectorAll('.stage-dot').forEach(el => {
        el.classList.remove('active', 'complete');
    });
}

function updateProgress(pct, text) {
    if (pct !== null) document.getElementById('progress-fill').style.width = pct + '%';
    document.getElementById('progress-text').textContent = text;
}

function hideProgress() {
    document.getElementById('progress-section').classList.add('hidden');
}

function setActiveStage(stage) {
    document.querySelectorAll('.stage-dot').forEach(el => {
        if (el.dataset.stage === stage) {
            el.classList.add('active');
            el.classList.remove('complete');
        }
    });
}

function completeStage(stage) {
    document.querySelectorAll('.stage-dot').forEach(el => {
        if (el.dataset.stage === stage) {
            el.classList.remove('active');
            el.classList.add('complete');
        }
    });
}

// ── Summary Bar ─────────────────────────────────
function renderSummary(policies) {
    const bar = document.getElementById('summary-bar');
    const active = policies.filter(p => (p.status || '').toUpperCase() === 'ACTIVE').length;
    const expiringSoon = policies.filter(p => {
        if (!p.policy_end) return false;
        const days = daysUntil(p.policy_end);
        return days !== null && days > 0 && days <= 90;
    }).length;
    const totalPremium = policies.reduce((sum, p) => {
        if (!p.premium || (p.status || '').toUpperCase() === 'EXPIRED') return sum;
        if (p.premium_frequency === 'one_time' && p.policy_start && p.policy_end) {
            const years = Math.max(1, Math.round((new Date(p.policy_end) - new Date(p.policy_start)) / (365.25 * 86400000)));
            return sum + (p.premium / years);
        }
        if (p.premium_frequency === 'monthly') return sum + (p.premium * 12);
        if (p.premium_frequency === 'quarterly') return sum + (p.premium * 4);
        return sum + p.premium;
    }, 0);

    bar.classList.remove('hidden');
    bar.innerHTML = `
        <div class="summary-item">
            <div class="summary-value">${policies.length}</div>
            <div class="summary-label">Total Policies</div>
        </div>
        <div class="summary-item">
            <div class="summary-value">${active}</div>
            <div class="summary-label">Active</div>
        </div>
        <div class="summary-item">
            <div class="summary-value">${expiringSoon}</div>
            <div class="summary-label">Expiring Soon</div>
        </div>
        <div class="summary-item">
            <div class="summary-value">${formatCurrency(totalPremium)}</div>
            <div class="summary-label">Annual Premium</div>
        </div>
    `;
}

// ── Render Policy Cards (Compact) ──────────────
function renderPolicies(policies) {
    const container = document.getElementById('policies-container');
    policies.sort((a, b) => {
        const daysA = a.policy_end ? daysUntil(a.policy_end) : null;
        const daysB = b.policy_end ? daysUntil(b.policy_end) : null;
        const bucketA = daysA !== null && daysA > 0 && daysA <= 90 ? 0 : (a.status || '').toUpperCase() === 'ACTIVE' ? 1 : 2;
        const bucketB = daysB !== null && daysB > 0 && daysB <= 90 ? 0 : (b.status || '').toUpperCase() === 'ACTIVE' ? 1 : 2;
        if (bucketA !== bucketB) return bucketA - bucketB;
        // Within same bucket, soonest expiry first
        const dateA = a.policy_end ? new Date(a.policy_end) : new Date('9999-12-31');
        const dateB = b.policy_end ? new Date(b.policy_end) : new Date('9999-12-31');
        return dateA - dateB;
    });
    container.innerHTML = policies.map((p, i) => renderCard(p, i)).join('');
}

function renderCard(p, index) {
    const typeLabels = { health: 'Health', car: 'Car', term_life: 'Term Life' };
    const type = p.type || 'unknown';
    const isLocked = p.password_protected === true;
    const status = (p.status || 'UNKNOWN').toUpperCase();
    const badgeClass = status === 'ACTIVE' ? 'badge-active' : status === 'EXPIRED' ? 'badge-expired' : 'badge-unknown';

    const days = p.policy_end ? daysUntil(p.policy_end) : null;
    let daysHtml = '';
    if (days !== null) {
        if (days <= 0) {
            daysHtml = `<span class="days-danger">Expired</span>`;
        } else if (days <= 90) {
            daysHtml = `<span class="days-warning">${days}d</span>`;
        } else {
            daysHtml = `<span class="days-ok">${days}d</span>`;
        }
    } else {
        daysHtml = `<span>---</span>`;
    }

    const hideIcon = currentFilter === 'hidden' ? '+' : '\u00d7';
    const hideTitle = currentFilter === 'hidden' ? 'Restore policy' : 'Hide policy';
    const hideBtn = p.policy_number ? `<span class="card-hide-btn" title="${hideTitle}" onclick="toggleHidePolicy(${index}, event)">${hideIcon}</span>` : '';

    if (isLocked) {
        return `
        <div class="policy-card locked" data-type="${type}" onclick="openModal(${index})">
            ${hideBtn}
            <div class="card-top">
                <span class="type-icon ${type}">${typeLabels[type] ? typeLabels[type][0] : '?'}</span>
                <span class="badge badge-locked">LOCKED</span>
            </div>
            <div class="card-name">${p.provider || 'Unknown Provider'}</div>
            <div class="card-locked-msg">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg>
                PDF is password-protected
            </div>
            <div class="card-locked-detail">We found this policy but couldn't read the PDF. Check your email for the password.</div>
            <div class="card-pn">${p.policy_number || '---'}</div>
        </div>`;
    }

    return `
    <div class="policy-card" data-type="${type}" onclick="openModal(${index})">
        ${hideBtn}
        <div class="card-top">
            <span class="type-icon ${type}">${typeLabels[type] ? typeLabels[type][0] : '?'}</span>
            <span class="badge ${badgeClass}">${status}</span>
        </div>
        <div class="card-name">${p.plan_name || p.provider || 'Unknown Policy'}</div>
        <div class="card-stats">
            <div>
                <div class="card-stat-label">Premium</div>
                <div class="card-stat-value">${formatCurrency(p.premium)}</div>
            </div>
            <div>
                <div class="card-stat-label">Sum Insured</div>
                <div class="card-stat-value">${formatCurrency(p.sum_insured)}</div>
            </div>
            <div>
                <div class="card-stat-label">Days Left</div>
                <div class="card-stat-value">${daysHtml}</div>
            </div>
            <div>
                <div class="card-stat-label">Frequency</div>
                <div class="card-stat-value">${p.premium_frequency || '---'}</div>
            </div>
        </div>
        <div class="card-pn">${p.policy_number || '---'}</div>
    </div>`;
}

// ── Modal ───────────────────────────────────────
function openModal(index) {
    const filtered = getFilteredPolicies();
    const p = filtered[index];
    if (!p) return;

    const typeLabels = { health: 'Health', car: 'Car', term_life: 'Term Life' };
    const members = (p.insured_members || [])
        .map(m => `${m.name}${m.relationship ? ' (' + m.relationship + ')' : ''}`)
        .join(', ');

    let rows = '';
    if (p.password_protected) {
        const hint = p.password_hint || 'Usually your date of birth (DDMMYYYY) or PAN number';
        rows += `<div class="modal-locked-banner">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg>
            This policy's PDF is password-protected. Enter the password below to unlock full details.
        </div>
        <div class="unlock-form" id="unlock-form">
            <div class="unlock-hint">Hint: ${hint}</div>
            <div class="unlock-input-row">
                <input type="text" id="unlock-password" class="unlock-input" placeholder="Enter PDF password" autocomplete="off" />
                <button class="unlock-btn" onclick="unlockPdf(${index})">Unlock</button>
            </div>
            <div class="unlock-error hidden" id="unlock-error"></div>
        </div>`;
    }
    rows += propRow('Provider', p.provider);
    rows += propRow('Plan', p.plan_name);
    rows += propRow('Policy No', p.policy_number);
    rows += propRow('Type', typeLabels[p.type] || p.type);
    rows += propRow('Status', p.status);
    if (members) rows += propRow('Insured', members);
    rows += propRow('Sum Insured', formatCurrency(p.sum_insured));
    rows += propRow('Premium', p.premium ? formatCurrency(p.premium) + '/' + (p.premium_frequency || 'year') : null);
    rows += propRow('Period', (p.policy_start || p.policy_end) ? formatDate(p.policy_start) + ' \u2192 ' + formatDate(p.policy_end) : null);
    if (p.vehicle) rows += propRow('Vehicle', `${p.vehicle.make || ''} ${p.vehicle.model || ''} (${p.vehicle.registration || ''})`);
    if (p.nominee) rows += propRow('Nominee', `${p.nominee.name} (${p.nominee.relationship})`);
    if (p.intermediary) rows += propRow('Intermediary', p.intermediary);
    if (p.coverages && p.coverages.length) rows += propRow('Coverages', p.coverages.join(', '), true);
    if (p.notes) rows += propRow('Notes', p.notes, true);
    if (p.source_msg_id) {
        const gmailUrl = `https://mail.google.com/mail/u/0/#inbox/${p.source_msg_id}`;
        rows += propRow('Source Email', `<span class="tooltip-wrap"><a href="${gmailUrl}" target="_blank" rel="noopener">${p.source_email || 'View in Gmail'}</a><span class="tooltip-text">If the link opens the wrong account, change /u/0/ in the URL to /u/1/, /u/2/, etc.</span></span>`);
    }

    const container = document.getElementById('modal-container');
    container.innerHTML = `
    <div class="modal-overlay" onclick="closeModal(event)">
        <div class="modal" onclick="event.stopPropagation()">
            <div class="modal-header">
                <h2>${p.plan_name || p.provider || 'Policy Details'}</h2>
                <button class="modal-close" onclick="closeModal()">\u00d7</button>
            </div>
            <div class="modal-body">${rows}</div>
        </div>
    </div>`;

    // Focus the password input if present
    const pwInput = document.getElementById('unlock-password');
    if (pwInput) {
        pwInput.focus();
        pwInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') unlockPdf(index);
        });
    }
}

async function unlockPdf(index) {
    const filtered = getFilteredPolicies();
    const p = filtered[index];
    if (!p) return;

    const password = document.getElementById('unlock-password').value.trim();
    if (!password) return;

    const btn = document.querySelector('.unlock-btn');
    const errEl = document.getElementById('unlock-error');
    btn.disabled = true;
    btn.textContent = 'Unlocking...';
    errEl.classList.add('hidden');

    try {
        const res = await fetch('/api/policies/unlock', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                pdf_path: p.locked_pdf_path,
                password: password,
                email_subject: p.source_email || '',
                vault_key: 'Ashish',
            }),
        });

        const data = await res.json();

        if (!res.ok) {
            errEl.textContent = data.error || 'Unlock failed';
            errEl.classList.remove('hidden');
            btn.disabled = false;
            btn.textContent = 'Unlock';
            return;
        }

        // Close modal and reload policies from server (cache was updated)
        closeModal();
        await loadPolicies();

    } catch (e) {
        errEl.textContent = 'Network error. Please try again.';
        errEl.classList.remove('hidden');
        btn.disabled = false;
        btn.textContent = 'Unlock';
    }
}

function closeModal(event) {
    if (event && event.target !== event.currentTarget) return;
    document.getElementById('modal-container').innerHTML = '';
}

function getFilteredPolicies() {
    const filtered = allPolicies.filter(p => {
        const isHidden = hiddenPolicies.has(p.policy_number);
        if (currentFilter === 'hidden') return isHidden;
        if (isHidden) return false;

        const status = (p.status || '').toUpperCase();
        const days = p.policy_end ? daysUntil(p.policy_end) : null;
        switch (currentFilter) {
            case 'active': return status === 'ACTIVE';
            case 'expiring': return days !== null && days > 0 && days <= 90;
            case 'inactive': return status === 'EXPIRED' || (days !== null && days <= 0);
            default: return true;
        }
    });
    filtered.sort((a, b) => {
        const daysA = a.policy_end ? daysUntil(a.policy_end) : null;
        const daysB = b.policy_end ? daysUntil(b.policy_end) : null;
        const bucketA = daysA !== null && daysA > 0 && daysA <= 90 ? 0 : (a.status || '').toUpperCase() === 'ACTIVE' ? 1 : 2;
        const bucketB = daysB !== null && daysB > 0 && daysB <= 90 ? 0 : (b.status || '').toUpperCase() === 'ACTIVE' ? 1 : 2;
        if (bucketA !== bucketB) return bucketA - bucketB;
        const dateA = a.policy_end ? new Date(a.policy_end) : new Date('9999-12-31');
        const dateB = b.policy_end ? new Date(b.policy_end) : new Date('9999-12-31');
        return dateA - dateB;
    });
    return filtered;
}

function propRow(label, value, muted) {
    if (!value) return '';
    return `<div class="prop-row">
        <span class="prop-label">${label}</span>
        <span class="prop-value${muted ? ' muted' : ''}">${value}</span>
    </div>`;
}

// ── Cache Info ──────────────────────────────────
function updateCacheInfo(fetchedAt, fromCache) {
    const el = document.getElementById('cache-info');
    if (!fetchedAt) { el.textContent = ''; return; }

    const fetched = new Date(fetchedAt);
    const ago = timeAgo(fetched);

    if (fromCache) {
        el.textContent = `Cached \u00b7 last updated ${ago}`;
    } else {
        el.textContent = `Just refreshed`;
    }
}

// ── Toast ───────────────────────────────────────
function showToast(msg) {
    const existing = document.querySelector('.toast');
    if (existing) existing.remove();

    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.textContent = msg;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 5000);
}

// ── Helpers ─────────────────────────────────────
function formatCurrency(amount) {
    if (!amount) return '---';
    return '\u20B9' + Number(amount).toLocaleString('en-IN');
}

function formatDate(d) {
    if (!d) return '---';
    try {
        return new Date(d).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' });
    } catch {
        return d;
    }
}

function daysUntil(dateStr) {
    if (!dateStr) return null;
    try {
        const diff = new Date(dateStr) - new Date();
        return Math.ceil(diff / 86400000);
    } catch {
        return null;
    }
}

function timeAgo(date) {
    const seconds = Math.floor((new Date() - date) / 1000);
    if (seconds < 60) return 'just now';
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    if (days === 1) return 'yesterday';
    return `${days}d ago`;
}

// ── Keyboard ────────────────────────────────────
document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeModal();
    // Arrow keys for onboarding slider
    const loginScreen = document.getElementById('login-screen');
    if (loginScreen && !loginScreen.classList.contains('hidden')) {
        if (e.key === 'ArrowRight') slideNav(1);
        if (e.key === 'ArrowLeft') slideNav(-1);
    }
});

// ── Onboarding Slider ──────────────────────────
let currentSlide = 0;
const totalSlides = 4;

function goToSlide(n) {
    currentSlide = n;
    document.querySelectorAll('.slide').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.dot').forEach(el => el.classList.remove('active'));

    const slide = document.querySelector(`.slide[data-slide="${n}"]`);
    const dot = document.querySelectorAll('.dot')[n];
    if (slide) slide.classList.add('active');
    if (dot) dot.classList.add('active');

    const prev = document.getElementById('slide-prev');
    const next = document.getElementById('slide-next');
    if (prev) prev.disabled = n === 0;
    if (next) {
        if (n === totalSlides - 1) {
            next.style.opacity = '0';
            next.style.pointerEvents = 'none';
        } else {
            next.style.opacity = '';
            next.style.pointerEvents = '';
            next.disabled = false;
        }
    }
}

function slideNav(dir) {
    const next = currentSlide + dir;
    if (next >= 0 && next < totalSlides) goToSlide(next);
}

// ── Go ──────────────────────────────────────────
init();
