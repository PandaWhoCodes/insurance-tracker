// ── Logout ──────────────────────────────────────
function doLogout() {
    sessionStorage.removeItem('vault_key');
    window.location.href = '/auth/logout';
}

// ── API ─────────────────────────────────────────
const API = {
    me: () => fetch('/api/me').then(r => r.json()),
    policies: (vaultKey) => {
        const vk = vaultKey || sessionStorage.getItem('vault_key') || '';
        return fetch('/api/policies?vault_key=' + encodeURIComponent(vk)).then(r => r.ok ? r.json() : null);
    },
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
let hasGmailScope = false;
let currentRefreshEs = null;
let wakeLockSentinel = null;

// ── Vault Key ──────────────────────────────────
function getVaultKey() {
    const cached = sessionStorage.getItem('vault_key');
    if (cached) return Promise.resolve(cached);

    return new Promise((resolve) => {
        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay';
        overlay.innerHTML = `
        <div class="modal vault-key-modal" onclick="event.stopPropagation()">
            <div class="modal-header">
                <h2>Enter your vault key</h2>
                <button class="modal-close" id="vk-close">&times;</button>
            </div>
            <div class="modal-body">
                <p class="vk-description">Your vault key encrypts all policy data stored on our servers. Without it, your data is unreadable — even to us.</p>
                <div class="vk-warning">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
                    <span>If you lose your vault key, your data <strong>cannot be recovered</strong>. There is no reset option. Please remember it.</span>
                </div>
                <input type="text" id="vk-input" class="vk-input" placeholder="Enter your vault key" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" />
                <button class="btn btn-primary vk-submit" id="vk-submit">Continue</button>
            </div>
        </div>`;

        const mc = document.getElementById('modal-container');
        mc.appendChild(overlay);
        enableBottomSheetSwipe(mc);

        const input = document.getElementById('vk-input');
        const submit = document.getElementById('vk-submit');
        const close = document.getElementById('vk-close');

        input.focus();

        function confirm() {
            const key = input.value.trim();
            if (!key) { input.focus(); return; }
            sessionStorage.setItem('vault_key', key);
            overlay.remove();
            resolve(key);
        }

        function cancel() {
            overlay.remove();
            resolve(null);
        }

        submit.addEventListener('click', confirm);
        input.addEventListener('keydown', (e) => { if (e.key === 'Enter') confirm(); });
        close.addEventListener('click', cancel);
        overlay.addEventListener('click', (e) => { if (e.target === overlay) cancel(); });
    });
}

// ── Init ────────────────────────────────────────
async function init() {
    try {
        const user = await API.me();
        if (user.authenticated) {
            currentUserEmail = user.email;
            hasGmailScope = !!user.has_gmail;
            loadHiddenPolicies();
            updateLandingCtas(true);
            showMainScreen(user);
            updateGmailButton();
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

function updateLandingCtas(loggedIn) {
    document.querySelectorAll('.landing-cta').forEach(btn => {
        if (loggedIn) {
            btn.textContent = 'My Policies';
            btn.onclick = () => {
                document.getElementById('login-screen').classList.add('hidden');
                document.getElementById('main-screen').classList.remove('hidden');
            };
        }
    });
}

// ── Load Policies ───────────────────────────────
async function loadPolicies() {
    let data = await API.policies();

    // DB has encrypted data but no vault key provided — prompt for it
    if (data && data.need_vault_key) {
        const vk = await getVaultKey();
        if (vk) {
            data = await API.policies(vk);
        } else {
            data = null;
        }
    }

    // Wrong vault key — clear it and re-prompt
    if (data && data.wrong_key) {
        sessionStorage.removeItem('vault_key');
        showToast('Wrong vault key. Please try again.');
        const vk = await getVaultKey();
        if (vk) {
            data = await API.policies(vk);
            if (data && data.wrong_key) {
                sessionStorage.removeItem('vault_key');
                showToast('Wrong vault key. Upload a PDF or refresh from Gmail to start fresh.');
                data = null;
            }
        } else {
            data = null;
        }
    }

    if (data && data.policies && data.policies.length > 0) {
        allPolicies = data.policies.filter(p => p.policy_number || p.policy_end || p.password_protected);
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

// ── Gmail Button ───────────────────────────────
function updateGmailButton() {
    const btn = document.getElementById('refresh-btn');
    const mobileBtn = document.getElementById('mobile-refresh-btn');
    if (hasGmailScope) {
        const icon = '<svg viewBox="0 0 16 16" fill="currentColor" width="14" height="14"><path d="M8 3a5 5 0 1 0 4.546 2.914.5.5 0 0 1 .908-.417A6 6 0 1 1 8 2v1z"/><path d="M8 4.466V.534a.25.25 0 0 1 .41-.192l2.36 1.966c.12.1.12.284 0 .384L8.41 4.658A.25.25 0 0 1 8 4.466z"/></svg>';
        btn.innerHTML = icon + ' Refresh from Gmail';
        if (mobileBtn) mobileBtn.innerHTML = icon + ' <span>Refresh from Gmail</span>';
    } else {
        const icon = '<svg viewBox="0 0 16 16" fill="currentColor" width="14" height="14"><path d="M0 4a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H2a2 2 0 0 1-2-2V4zm2-1a1 1 0 0 0-1 1v.217l7 4.2 7-4.2V4a1 1 0 0 0-1-1H2zm13 2.383-4.708 2.825L15 11.105V5.383zm-.034 6.876-5.64-3.471L8 9.583l-1.326-.795-5.64 3.47A1 1 0 0 0 2 13h12a1 1 0 0 0 .966-.741zM1 11.105l4.708-2.897L1 5.383v5.722z"/></svg>';
        btn.innerHTML = icon + ' Connect Gmail';
        if (mobileBtn) mobileBtn.innerHTML = icon + ' <span>Connect Gmail</span>';
    }
}

// ── Refresh (SSE) ──────────────────────────────
async function refreshPolicies(forceRefresh = false) {
    if (isRefreshing) return;

    if (!hasGmailScope) {
        window.location.href = '/auth/gmail';
        return;
    }

    const vaultKey = await getVaultKey();
    if (!vaultKey) {
        showToast('Vault key is required to continue. Click the button to try again.');
        return;
    }

    isRefreshing = true;
    const _refreshStart = Date.now();

    const refreshBtn = document.getElementById('refresh-btn');
    refreshBtn.disabled = true;
    showProgress('Scanning your inbox...');
    setActiveStage('gmail');

    await requestWakeLock();

    let url = '/api/policies/refresh-stream?vault_key=' + encodeURIComponent(vaultKey);
    if (forceRefresh) url += '&force=true';
    const es = new EventSource(url);
    currentRefreshEs = es;

    es.addEventListener('progress', (e) => {
        const d = JSON.parse(e.data);
        updateProgress(d.pct, d.message);
        if (d.stage) setActiveStage(d.stage);
    });

    es.addEventListener('stage_complete', (e) => {
        const d = JSON.parse(e.data);
        updateProgress(d.pct || null, d.message);
        if (d.stage) completeStage(d.stage);
    });

    es.addEventListener('done', (e) => {
        const d = JSON.parse(e.data);
        es.close();
        completeStage('finalize');
        updateProgress(100, 'All done!');
        setTimeout(() => applyRefreshResults(d, refreshBtn), 400);
    });

    es.addEventListener('error_event', (e) => {
        const d = JSON.parse(e.data);
        es.close();
        hideProgress();
        releaseWakeLock();
        showToast('Refresh failed: ' + d.message);
        isRefreshing = false;
        currentRefreshEs = null;
        refreshBtn.disabled = false;
        if (d.message && (d.message.includes('re-authenticate') || d.message.includes('No credentials'))) {
            refreshBtn.textContent = 'Re-login';
            refreshBtn.onclick = () => { window.location.href = '/auth/login'; };
        }
    });

    es.onerror = () => {
        es.close();
        currentRefreshEs = null;
        pollForResults(vaultKey, refreshBtn);
    };
}

// ── Upload PDF ─────────────────────────────────
async function handlePdfUpload(input) {
    const file = input.files[0];
    input.value = ''; // reset so same file can be re-selected
    if (!file) return;

    const uploadBtn = document.getElementById('upload-btn');
    const origText = uploadBtn.innerHTML;
    uploadBtn.disabled = true;
    uploadBtn.innerHTML = '<span class="spinner-small"></span> Uploading...';

    try {
        const result = await uploadPdf(file, '');

        if (result.needs_password) {
            const password = prompt('This PDF is password-protected. Enter the password:');
            if (!password) {
                showToast('Upload cancelled.');
                return;
            }
            const retryResult = await uploadPdf(file, password);
            if (retryResult.error) {
                showToast(retryResult.error);
                return;
            }
            applyUploadResult(retryResult);
        } else if (result.error) {
            showToast(result.error);
        } else {
            applyUploadResult(result);
        }
    } catch (e) {
        showToast('Upload failed: ' + e.message);
    } finally {
        uploadBtn.disabled = false;
        uploadBtn.innerHTML = origText;
    }
}

async function uploadPdf(file, password) {
    const vaultKey = await getVaultKey();
    if (!vaultKey) return { error: 'Vault key is required. Please try uploading again.' };

    const form = new FormData();
    form.append('file', file);
    if (password) form.append('password', password);
    form.append('vault_key', vaultKey);

    const resp = await fetch('/api/policies/upload', { method: 'POST', body: form });
    const data = await resp.json();
    if (!resp.ok) return { error: data.error || 'Upload failed' };
    return data;
}

function applyUploadResult(data) {
    if (data.policies) {
        allPolicies = data.policies;
    } else if (data.policy) {
        allPolicies.push(data.policy);
    }
    const visible = allPolicies.filter(p => !hiddenPolicies.has(p.policy_number));
    renderSummary(visible);
    renderFiltered();
    showToast('Policy added from uploaded PDF.');
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

// ── Refresh Results Helper ──────────────────────
function applyRefreshResults(data, refreshBtn) {
    hideProgress();
    releaseWakeLock();
    const complete = (data.policies || []).filter(p => p.policy_number || p.policy_end || p.password_protected);
    if (complete.length > 0) {
        allPolicies = complete;
        const visible = allPolicies.filter(p => !hiddenPolicies.has(p.policy_number));
        renderSummary(visible);
        renderFiltered();
        updateCacheInfo(data.fetched_at, false);
        document.getElementById('empty-state').classList.add('hidden');
        document.getElementById('filter-bar').classList.remove('hidden');
    } else {
        document.getElementById('empty-state').classList.remove('hidden');
        document.getElementById('policies-container').innerHTML = '';
        document.getElementById('summary-bar').classList.add('hidden');
        document.getElementById('filter-bar').classList.add('hidden');
    }
    isRefreshing = false;
    currentRefreshEs = null;
    refreshBtn.disabled = false;
}

// ── Poll for results (mobile SSE recovery) ──────
async function pollForResults(vaultKey, refreshBtn) {
    updateProgress(null, 'Reconnecting...');
    for (let i = 0; i < 10; i++) {
        await new Promise(r => setTimeout(r, 3000));
        try {
            const data = await API.policies(vaultKey);
            if (data && data.policies && data.policies.length > 0) {
                applyRefreshResults(data, refreshBtn);
                return;
            }
        } catch {}
    }
    hideProgress();
    releaseWakeLock();
    showToast('Your data may still be processing — try reloading in a minute.');
    isRefreshing = false;
    currentRefreshEs = null;
    refreshBtn.disabled = false;
}

// ── Wake Lock (prevent screen off during refresh) ──
async function requestWakeLock() {
    try {
        if (navigator.wakeLock) {
            wakeLockSentinel = await navigator.wakeLock.request('screen');
        }
    } catch {}
}

function releaseWakeLock() {
    if (wakeLockSentinel) {
        wakeLockSentinel.release().catch(() => {});
        wakeLockSentinel = null;
    }
}

// ── Premium Calculation ─────────────────────────
function annualizePremium(p) {
    if (!p.premium || (p.status || '').toUpperCase() === 'EXPIRED') {
        return { annual: 0, explanation: 'Expired — not counted' };
    }
    const freq = (p.premium_frequency || '').toLowerCase();
    if (freq === 'monthly') return { annual: p.premium * 12, explanation: `${formatCurrency(p.premium)} × 12 months` };
    if (freq === 'quarterly') return { annual: p.premium * 4, explanation: `${formatCurrency(p.premium)} × 4 quarters` };
    if (freq === 'half-yearly' || freq === 'semi-annual') return { annual: p.premium * 2, explanation: `${formatCurrency(p.premium)} × 2` };
    if (freq === 'yearly' || freq === 'annual') return { annual: p.premium, explanation: `${formatCurrency(p.premium)}/year` };
    if (p.policy_start && p.policy_end) {
        const years = Math.max(1, Math.round((new Date(p.policy_end) - new Date(p.policy_start)) / (365.25 * 86400000)));
        if (years > 1) return { annual: p.premium / years, explanation: `${formatCurrency(p.premium)} ÷ ${years} years` };
    }
    return { annual: p.premium, explanation: `${formatCurrency(p.premium)}/year` };
}

// ── Export to PDF ──────────────────────────────
function exportPDF() {
    if (!window.jspdf) { showToast('PDF library not loaded. Please try again.'); return; }
    const { jsPDF } = window.jspdf;
    const doc = new jsPDF('p', 'mm', 'a4');
    const pageW = doc.internal.pageSize.getWidth();
    const pageH = doc.internal.pageSize.getHeight();
    const margin = 16;
    const contentW = pageW - margin * 2;
    let y = margin;

    const policies = allPolicies.filter(p => !hiddenPolicies.has(p.policy_number));
    if (!policies.length) { showToast('No policies to export.'); return; }

    // Sort: Active → Expiring Soon → Expired
    const statusOrder = { 'ACTIVE': 0, 'EXPIRED': 2 };
    policies.sort((a, b) => {
        const aStatus = (a.status || 'UNKNOWN').toUpperCase();
        const bStatus = (b.status || 'UNKNOWN').toUpperCase();
        let aOrder = statusOrder[aStatus] ?? 1;
        let bOrder = statusOrder[bStatus] ?? 1;
        const aDays = daysUntil(a.policy_end);
        const bDays = daysUntil(b.policy_end);
        if (aStatus === 'ACTIVE' && aDays !== null && aDays <= 90) aOrder = 0.5;
        if (bStatus === 'ACTIVE' && bDays !== null && bDays <= 90) bOrder = 0.5;
        return aOrder - bOrder;
    });

    // Summary stats
    const active = policies.filter(p => (p.status || '').toUpperCase() === 'ACTIVE').length;
    const expiring = policies.filter(p => {
        const d = daysUntil(p.policy_end);
        return d !== null && d > 0 && d <= 90;
    }).length;
    const totalPremium = Math.round(policies.reduce((s, p) => s + annualizePremium(p).annual, 0));

    function drawHeader() {
        doc.setFontSize(18);
        doc.setFont('helvetica', 'bold');
        doc.setTextColor(30, 30, 30);
        doc.text('Policies.life', margin, y);
        doc.setFontSize(9);
        doc.setFont('helvetica', 'normal');
        doc.setTextColor(120);
        doc.text(new Date().toLocaleDateString('en-IN', { day: 'numeric', month: 'long', year: 'numeric' }), pageW - margin, y, { align: 'right' });
        y += 3;
        doc.setDrawColor(220);
        doc.line(margin, y, pageW - margin, y);
        y += 6;
    }

    function checkPageBreak(needed) {
        if (y + needed > pageH - 20) {
            doc.addPage();
            y = margin;
            drawHeader();
        }
    }

    function fmtCurrency(amt) {
        if (!amt) return '---';
        return 'Rs ' + Number(amt).toLocaleString('en-IN');
    }

    function fmtDate(d) {
        if (!d) return '---';
        try { return new Date(d).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' }); }
        catch { return d; }
    }

    // Page 1: header + user + summary
    drawHeader();
    if (currentUserEmail) {
        doc.setFontSize(9);
        doc.setTextColor(120);
        doc.text(currentUserEmail, margin, y);
        y += 6;
    }
    doc.setFontSize(10);
    doc.setFont('helvetica', 'normal');
    doc.setTextColor(80);
    let summaryText = `${active} Active`;
    if (expiring) summaryText += `  |  ${expiring} Expiring Soon`;
    summaryText += `  |  Annual Premium: ${fmtCurrency(totalPremium)}`;
    doc.text(summaryText, margin, y);
    y += 10;

    // Render each policy
    policies.forEach((p) => {
        checkPageBreak(40);

        // Section header
        const status = (p.status || 'UNKNOWN').toUpperCase();
        const title = [p.provider, p.plan_name].filter(Boolean).join(' \u2014 ') || 'Unknown Policy';
        doc.setFontSize(11);
        doc.setFont('helvetica', 'bold');
        doc.setTextColor(30);
        doc.text(title, margin, y, { maxWidth: contentW - 30 });
        doc.setFontSize(8);
        doc.setFont('helvetica', 'normal');
        const statusColor = status === 'ACTIVE' ? [34, 139, 34] : status === 'EXPIRED' ? [180, 60, 60] : [120, 120, 120];
        doc.setTextColor(...statusColor);
        doc.text(status, pageW - margin, y, { align: 'right' });
        y += 2;

        if (p.password_protected) {
            const rows = [['Status', 'Password Protected']];
            if (p.policy_number) rows.unshift(['Policy Number', p.policy_number]);
            doc.autoTable({
                startY: y, margin: { left: margin, right: margin }, body: rows, theme: 'plain',
                styles: { fontSize: 9, cellPadding: 2, textColor: [60, 60, 60] },
                columnStyles: { 0: { fontStyle: 'bold', cellWidth: 38, textColor: [100, 100, 100] } },
            });
            y = doc.lastAutoTable.finalY + 8;
            return;
        }

        // Build key-value rows
        const rows = [];
        if (p.policy_number) rows.push(['Policy Number', p.policy_number]);
        if (p.type) rows.push(['Type', p.type.replace('_', ' ').replace(/\b\w/g, c => c.toUpperCase())]);
        if (p.policy_start || p.policy_end) {
            let period = fmtDate(p.policy_start) + '  \u2192  ' + fmtDate(p.policy_end);
            const days = daysUntil(p.policy_end);
            if (days !== null && status === 'ACTIVE') period += `  (${days} days left)`;
            rows.push(['Period', period]);
        }
        if (p.sum_insured) rows.push(['Sum Insured', fmtCurrency(p.sum_insured)]);
        if (p.premium) {
            let premStr = fmtCurrency(p.premium);
            if (p.premium_frequency) premStr += ' / ' + p.premium_frequency;
            rows.push(['Premium', premStr]);
        }
        if (p.insured_members && p.insured_members.length) {
            const members = p.insured_members.map(m => {
                let s = m.name || 'Unknown';
                if (m.relationship) s += ` (${m.relationship})`;
                if (m.date_of_birth) s += ` \u2014 DOB: ${fmtDate(m.date_of_birth)}`;
                return s;
            }).join('\n');
            rows.push(['Insured', members]);
        }
        if (p.vehicle) {
            const parts = [p.vehicle.make, p.vehicle.model, p.vehicle.registration].filter(Boolean);
            if (parts.length) rows.push(['Vehicle', parts.join(' \u2014 ')]);
        }
        if (p.nominee) {
            let nom = p.nominee.name || '';
            if (p.nominee.relationship) nom += ` (${p.nominee.relationship})`;
            if (nom) rows.push(['Nominee', nom]);
        }
        if (p.intermediary) rows.push(['Intermediary', p.intermediary]);
        if (p.coverages && p.coverages.length) rows.push(['Coverages', p.coverages.join(', ')]);
        if (p.notes) rows.push(['Notes', p.notes]);

        doc.autoTable({
            startY: y, margin: { left: margin, right: margin }, body: rows, theme: 'plain',
            styles: { fontSize: 9, cellPadding: 2.5, textColor: [60, 60, 60], overflow: 'linebreak' },
            columnStyles: {
                0: { fontStyle: 'bold', cellWidth: 38, textColor: [100, 100, 100] },
                1: { cellWidth: contentW - 38 },
            },
        });
        y = doc.lastAutoTable.finalY + 8;
    });

    // Page numbers
    const totalPages = doc.internal.getNumberOfPages();
    for (let i = 1; i <= totalPages; i++) {
        doc.setPage(i);
        doc.setFontSize(8);
        doc.setTextColor(160);
        doc.text(`Page ${i} of ${totalPages}`, pageW / 2, pageH - 10, { align: 'center' });
    }

    doc.save('policies-life-export.pdf');
}

function showPremiumBreakdown() {
    const visible = allPolicies.filter(p => !hiddenPolicies.has(p.policy_number));
    const rows = visible
        .map(p => ({ policy: p, ...annualizePremium(p) }))
        .filter(r => r.policy.premium);

    const total = rows.reduce((s, r) => s + r.annual, 0);

    const rowsHtml = rows.map(r => {
        const name = r.policy.plan_name || r.policy.provider || 'Unknown';
        const isExpired = (r.policy.status || '').toUpperCase() === 'EXPIRED';
        return `<tr class="${isExpired ? 'premium-row-expired' : ''}">
            <td class="pb-name">${name}</td>
            <td class="pb-calc">${r.explanation}</td>
            <td class="pb-amount">${isExpired ? '—' : formatCurrency(Math.round(r.annual))}</td>
        </tr>`;
    }).join('');

    const container = document.getElementById('modal-container');
    container.innerHTML = `
    <div class="modal-overlay" onclick="closeModal(event)">
        <div class="modal premium-modal" onclick="event.stopPropagation()">
            <div class="modal-header">
                <h2>Annual Premium Breakdown</h2>
                <button class="modal-close" onclick="closeModal()">&times;</button>
            </div>
            <div class="modal-body" style="padding: 0;">
                <table class="premium-table">
                    <thead>
                        <tr><th>Policy</th><th>Calculation</th><th>Per Year</th></tr>
                    </thead>
                    <tbody>${rowsHtml}</tbody>
                    <tfoot>
                        <tr><td colspan="2">Total Annual Premium</td><td class="pb-amount">${formatCurrency(Math.round(total))}</td></tr>
                    </tfoot>
                </table>
            </div>
        </div>
    </div>`;
    enableBottomSheetSwipe(container);
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
    const totalPremium = policies.reduce((sum, p) => sum + annualizePremium(p).annual, 0);

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
        <div class="summary-item clickable" onclick="showPremiumBreakdown()">
            <div class="summary-value">${formatCurrency(Math.round(totalPremium))}</div>
            <div class="summary-label">Annual Premium</div>
        </div>
    `;

    // Export button inside filter bar (right-aligned)
    const filterBar = document.getElementById('filter-bar');
    if (filterBar && !filterBar.querySelector('.export-btn')) {
        const btn = document.createElement('button');
        btn.className = 'btn btn-ghost export-btn';
        btn.onclick = exportPDF;
        btn.innerHTML = `<svg viewBox="0 0 16 16" fill="currentColor" width="14" height="14"><path d="M.5 9.9a.5.5 0 0 1 .5.5v2.5a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-2.5a.5.5 0 0 1 1 0v2.5a2 2 0 0 1-2 2H2a2 2 0 0 1-2-2v-2.5a.5.5 0 0 1 .5-.5z"/><path d="M7.646 11.854a.5.5 0 0 0 .708 0l3-3a.5.5 0 0 0-.708-.708L8.5 10.293V1.5a.5.5 0 0 0-1 0v8.793L5.354 8.146a.5.5 0 1 0-.708.708l3 3z"/></svg> Export PDF`;
        filterBar.appendChild(btn);
    }
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
                <input type="text" id="unlock-password" class="unlock-input" placeholder="Enter PDF password" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" />
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

    enableBottomSheetSwipe(container);

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
        const vaultKey = await getVaultKey();
        if (!vaultKey) { btn.disabled = false; btn.textContent = 'Unlock'; return; }

        const res = await fetch('/api/policies/unlock', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                pdf_path: p.locked_pdf_path,
                password: password,
                email_subject: p.source_email || '',
                vault_key: vaultKey,
                msg_id: p.source_msg_id || '',
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
});

// ── Mobile Menu ─────────────────────────────────
function toggleMobileMenu() {
    const menu = document.getElementById('mobile-menu');
    if (menu) menu.classList.toggle('active');
}

function closeMobileMenu() {
    const menu = document.getElementById('mobile-menu');
    if (menu) menu.classList.remove('active');
}

document.addEventListener('click', (e) => {
    const menu = document.getElementById('mobile-menu');
    const btn = document.getElementById('mobile-menu-btn');
    if (menu && btn && !menu.contains(e.target) && !btn.contains(e.target)) {
        closeMobileMenu();
    }
});

document.addEventListener('DOMContentLoaded', () => {
    const btn = document.getElementById('mobile-menu-btn');
    if (btn) btn.addEventListener('click', toggleMobileMenu);
});

// ── Bottom Sheet Swipe ──────────────────────────
function enableBottomSheetSwipe(container) {
    const overlay = container.querySelector('.modal-overlay');
    const modal = overlay ? overlay.querySelector('.modal') : null;
    if (!modal || window.innerWidth > 428) return;

    const header = modal.querySelector('.modal-header');
    if (!header) return;

    let startY = 0, currentY = 0, isDragging = false;

    header.addEventListener('touchstart', (e) => {
        startY = e.touches[0].clientY;
        currentY = startY;
        isDragging = true;
        modal.style.transition = 'none';
    }, { passive: true });

    header.addEventListener('touchmove', (e) => {
        if (!isDragging) return;
        currentY = e.touches[0].clientY;
        const diff = currentY - startY;
        if (diff > 0) modal.style.transform = `translateY(${diff}px)`;
    }, { passive: true });

    header.addEventListener('touchend', () => {
        if (!isDragging) return;
        isDragging = false;
        modal.style.transition = 'transform 0.2s ease';
        const diff = currentY - startY;
        if (diff > 100) {
            modal.style.transform = 'translateY(100%)';
            setTimeout(() => closeModal(), 200);
        } else {
            modal.style.transform = 'translateY(0)';
        }
    });
}

// ── Visibility Recovery (mobile screen wake) ────
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && isRefreshing && currentRefreshEs && currentRefreshEs.readyState === EventSource.CLOSED) {
        const vaultKey = sessionStorage.getItem('vault_key');
        const refreshBtn = document.getElementById('refresh-btn');
        if (vaultKey && refreshBtn) {
            currentRefreshEs = null;
            pollForResults(vaultKey, refreshBtn);
        }
    }
});

// ── Go ──────────────────────────────────────────
init();
