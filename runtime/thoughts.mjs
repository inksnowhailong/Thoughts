#!/usr/bin/env node
import { readFileSync, writeFileSync, existsSync, mkdirSync, readdirSync, appendFileSync } from 'fs'
import { join, resolve } from 'path'
import { execSync } from 'child_process'
import { randomUUID } from 'crypto'

const HOME = process.env.HOME
const THOUGHTS_DIR = join(HOME, '.thoughts')
const INSTANCES_DIR = join(THOUGHTS_DIR, 'instances')
const ACTIVE_FILE = join(THOUGHTS_DIR, 'active.json')
const RUNTIME_DIR = join(THOUGHTS_DIR, 'runtime')

function getProjectPath() {
    try { return execSync('git rev-parse --show-toplevel 2>/dev/null', { encoding: 'utf8' }).trim() }
    catch { return process.cwd() }
}

function readJSON(path) {
    try { return JSON.parse(readFileSync(path, 'utf8')) }
    catch { return null }
}

function writeJSON(path, data) {
    writeFileSync(path, JSON.stringify(data, null, 2) + '\n')
}

function appendJSONL(path, obj) {
    appendFileSync(path, JSON.stringify(obj) + '\n')
}

function now() { return new Date().toISOString() }

function getInstanceDir(name) { return join(INSTANCES_DIR, name) }

function getCurrentInstance() {
    const active = readJSON(ACTIVE_FILE)
    if (!active) return null
    const project = getProjectPath()
    const name = active[project]
    if (!name) return null
    return { name, dir: getInstanceDir(name) }
}

function output(data) { process.stdout.write(JSON.stringify(data, null, 2) + '\n') }

// --- Command Router ---
const [,, cmd, ...args] = process.argv

// === list-instances ===
function listInstances() {
    if (!existsSync(INSTANCES_DIR)) return output({ instances: [] })
    const dirs = readdirSync(INSTANCES_DIR, { withFileTypes: true })
        .filter(d => d.isDirectory()).map(d => d.name)
    output({ instances: dirs })
}

// === ensure-instance ===
function ensureInstance(name) {
    if (!name) { console.error('Usage: ensure-instance <name>'); process.exit(1) }
    const dir = getInstanceDir(name)
    mkdirSync(dir, { recursive: true })
    const defaults = {
        'memory-raw.md': '# 思绪记忆 - 原始\n\n',
        'memory-consolidated.md': '# 思绪记忆 - 整理\n\n',
        'memory-active.json': '[]',
        'memory-index.jsonl': '',
        'memory-sources.jsonl': '',
        'style-samples.jsonl': '',
        'activity-log.jsonl': '',
        'cron-state.json': '{}',
        'loop-state.json': JSON.stringify({
            lastUserAt: null, lastActiveAt: null, nextActiveAt: null,
            currentDelayMs: 900000, consecutiveIgnores: 0,
            consecutiveQuiets: 0, totalActives: 0, totalUserResponses: 0
        }, null, 2),
        'permissions.json': JSON.stringify({ signals: {
            time: 'always', workspace: 'always', gitStatus: 'always', devServers: 'always',
            systemStatus: 'ask', activeApp: 'ask', weather: 'ask',
            browserTabs: 'deny', clipboard: 'deny', calendar: 'deny', recentFiles: 'deny'
        }, pendingRequests: [] }, null, 2),
        'mind-state.json': JSON.stringify({
            personaState: { mood: 'neutral', energy: 0.7, socialBattery: 0.8, toneBias: 'neutral', currentAttitude: 'curious' },
            editorialPolicy: { coreStance: '', messageShape: { maxLength: 200, mustInclude: ['stance', 'aftertaste'], forbidden: ['pure-fact-dump', 'progress-inquiry'] } },
            threads: [], candidateQueue: [],
            selectionPolicy: { recentTopicBuckets: {}, recentModes: [], downrankList: [], modeWeights: { discovery: 0.35, ambient: 0.15, casual: 0.25, reflection: 0.15, quiet: 0.10 } },
            subconscious: { lastRunAt: null, minQueueSize: 3, maxQueueSize: 8 }
        }, null, 2)
    }
    for (const [file, content] of Object.entries(defaults)) {
        const p = join(dir, file)
        if (!existsSync(p)) writeFileSync(p, content)
    }
    output({ ok: true, dir })
}

// === bind ===
function bind(name) {
    if (!name) { console.error('Usage: bind <name>'); process.exit(1) }
    const dir = getInstanceDir(name)
    if (!existsSync(dir)) { console.error(`Instance "${name}" not found`); process.exit(1) }
    const active = readJSON(ACTIVE_FILE) || {}
    const project = getProjectPath()
    active[project] = name
    writeJSON(ACTIVE_FILE, active)
    output({ ok: true, project, instance: name })
}

// === unbind ===
function unbind() {
    const active = readJSON(ACTIVE_FILE) || {}
    const project = getProjectPath()
    if (!active[project]) { output({ ok: true, wasUnbound: true }); return }
    delete active[project]
    writeJSON(ACTIVE_FILE, active)
    output({ ok: true, project })
}

// === state ===
function state() {
    const inst = getCurrentInstance()
    if (!inst) { output({ active: false }); return }
    const loopState = readJSON(join(inst.dir, 'loop-state.json'))
    output({ active: true, instance: inst.name, dir: inst.dir, loopState })
}

// === context ===
function context() {
    const inst = getCurrentInstance()
    if (!inst) { output({ error: 'no active instance' }); process.exit(1) }
    const perms = readJSON(join(inst.dir, 'permissions.json')) || { signals: {} }
    const snapshot = {}
    const signals = perms.signals || {}
    try {
        if (signals.time === 'always') snapshot.time = new Date().toLocaleString()
        if (signals.workspace === 'always') snapshot.workspace = getProjectPath()
        if (signals.gitStatus === 'always') {
            try { snapshot.gitStatus = execSync('git status --short 2>/dev/null', { encoding: 'utf8' }).trim().slice(0, 500) } catch {}
        }
        if (signals.devServers === 'always') {
            try { snapshot.devServers = execSync('lsof -iTCP -sTCP:LISTEN -P 2>/dev/null | grep -v COMMAND | head -5', { encoding: 'utf8' }).trim() } catch {}
        }
        if (signals.systemStatus === 'always') {
            try { snapshot.uptime = execSync('uptime', { encoding: 'utf8' }).trim() } catch {}
        }
        if (signals.weather === 'always') snapshot.weatherHint = '(use WebSearch)'
    } catch {}
    output({ environmentSnapshot: snapshot, permissions: perms })
}

// === select-thought (Decision Card Engine) ===
function selectThought() {
    const inst = getCurrentInstance()
    if (!inst) { output({ error: 'no active instance' }); process.exit(1) }
    const mindState = readJSON(join(inst.dir, 'mind-state.json'))
    const personality = readJSON(join(inst.dir, 'personality.json'))
    const loopState = readJSON(join(inst.dir, 'loop-state.json'))
    if (!mindState) { output({ shouldSpeak: false, mode: 'quiet', reason: 'no mind-state' }); return }

    const queue = mindState.candidateQueue || []
    const policy = mindState.selectionPolicy || {}
    const recentModes = policy.recentModes || []
    const downrank = policy.downrankList || []
    const buckets = policy.recentTopicBuckets || {}

    if (queue.length === 0) {
        output({ shouldSpeak: false, mode: 'quiet', reason: 'empty-queue' }); return
    }

    // own-thought-first priority: longThread > personaMood > worldObservation > tasteReaction > associativeDrift
    const sourcePriority = { longThread: 5, personaMood: 4, worldObservation: 3, tasteReaction: 2, associativeDrift: 1 }

    const scored = queue.map(c => {
        let score = c.score || 0.5
        score += (sourcePriority[c.source] || 0) * 0.1
        // diversity: penalize if same mode as last
        if (recentModes.length > 0 && recentModes[recentModes.length - 1] === c.mode) score -= 0.3
        // penalize if same topic bucket appeared 2+ times in last 6
        const bucketCount = buckets[c.topic] || 0
        if (bucketCount >= 2) score -= 0.4
        // penalize downranked topics
        if (downrank.includes(c.topic)) score -= 0.5
        // cooldown check
        if (c.cooldownUntil && new Date(c.cooldownUntil) > new Date()) score -= 1.0
        return { ...c, finalScore: score }
    }).sort((a, b) => b.finalScore - a.finalScore)

    const best = scored[0]
    if (best.finalScore < 0.2) {
        output({ shouldSpeak: false, mode: 'quiet', reason: 'all-candidates-low-quality' }); return
    }
    output({ shouldSpeak: true, candidate: best, mode: best.mode })
}

// === consume-thought ===
function consumeThought(candidateId, mode, topic) {
    const inst = getCurrentInstance()
    if (!inst) { output({ error: 'no active instance' }); process.exit(1) }
    const msPath = join(inst.dir, 'mind-state.json')
    const mindState = readJSON(msPath)
    if (!mindState) { output({ error: 'no mind-state' }); process.exit(1) }

    mindState.candidateQueue = (mindState.candidateQueue || []).filter(c => c.id !== candidateId)
    const policy = mindState.selectionPolicy
    policy.recentModes = [...(policy.recentModes || []).slice(-5), mode]
    policy.recentTopicBuckets[topic] = ((policy.recentTopicBuckets[topic] || 0) + 1)
    // decay old buckets
    for (const k of Object.keys(policy.recentTopicBuckets)) {
        if (policy.recentTopicBuckets[k] > 0 && k !== topic) policy.recentTopicBuckets[k] -= 0.2
        if (policy.recentTopicBuckets[k] <= 0) delete policy.recentTopicBuckets[k]
    }
    // update thread cooldown
    for (const t of mindState.threads || []) {
        if (t.title === topic || t.id === candidateId) {
            t.lastTouchedAt = now()
            t.cooldownRounds = 2
        } else if (t.cooldownRounds > 0) {
            t.cooldownRounds--
        }
    }
    writeJSON(msPath, mindState)
    output({ ok: true, remainingQueue: mindState.candidateQueue.length })
}

// === record-active ===
function recordActive(mode, topic) {
    const inst = getCurrentInstance()
    if (!inst) { output({ error: 'no active instance' }); process.exit(1) }
    const lsPath = join(inst.dir, 'loop-state.json')
    const loopState = readJSON(lsPath) || {}
    const personality = readJSON(join(inst.dir, 'personality.json'))
    const rhythm = personality?.rhythm || { baseDelayMs: 900000, minDelayMs: 300000, maxDelayMs: 3600000, decayMultiplier: 1.5, boostMultiplier: 0.7, quietHours: [0, 7] }

    const isQuiet = mode === 'quiet'
    if (!isQuiet) {
        loopState.totalActives = (loopState.totalActives || 0) + 1
        loopState.lastActiveAt = now()
    } else {
        loopState.consecutiveQuiets = (loopState.consecutiveQuiets || 0) + 1
    }

    // calculate delayMs
    let delayMs = loopState.currentDelayMs || rhythm.baseDelayMs
    const lastUser = loopState.lastUserAt ? new Date(loopState.lastUserAt) : null
    const minutesSinceUser = lastUser ? (Date.now() - lastUser.getTime()) / 60000 : Infinity

    if (minutesSinceUser < 30) {
        delayMs = Math.max(rhythm.minDelayMs, delayMs * rhythm.boostMultiplier)
    } else if (loopState.consecutiveIgnores >= 3) {
        delayMs = rhythm.maxDelayMs
    } else if (loopState.consecutiveIgnores >= 2) {
        delayMs = Math.min(rhythm.maxDelayMs, delayMs * rhythm.decayMultiplier)
    }
    // quiet hours check
    const hour = new Date().getHours()
    if (hour >= rhythm.quietHours[0] && hour < rhythm.quietHours[1]) {
        delayMs = rhythm.maxDelayMs * 2
    }
    if (isQuiet) { /* quiet doesn't count as ignore */ }
    else { loopState.consecutiveIgnores = (loopState.consecutiveIgnores || 0) + 1 }

    loopState.currentDelayMs = delayMs
    loopState.nextActiveAt = new Date(Date.now() + delayMs).toISOString()
    writeJSON(lsPath, loopState)

    appendJSONL(join(inst.dir, 'activity-log.jsonl'), { time: now(), mode, topic, delayMs })
    output({ delayMs, nextActiveAt: loopState.nextActiveAt })
}

// === record-user ===
function recordUser() {
    const inst = getCurrentInstance()
    if (!inst) { output({ error: 'no active instance' }); process.exit(1) }
    const lsPath = join(inst.dir, 'loop-state.json')
    const loopState = readJSON(lsPath) || {}
    loopState.lastUserAt = now()
    loopState.consecutiveIgnores = 0
    loopState.totalUserResponses = (loopState.totalUserResponses || 0) + 1
    writeJSON(lsPath, loopState)
    output({ ok: true, lastUserAt: loopState.lastUserAt })
}

// === record-style-sample ===
function recordStyleSample(mode, topic, message) {
    const inst = getCurrentInstance()
    if (!inst) { output({ error: 'no active instance' }); process.exit(1) }
    const path = join(inst.dir, 'style-samples.jsonl')
    appendJSONL(path, { time: now(), mode, topic, message: (message || '').slice(0, 500) })
    output({ ok: true })
}

// === set-permission ===
function setPermission(signal, value) {
    const inst = getCurrentInstance()
    if (!inst) { output({ error: 'no active instance' }); process.exit(1) }
    const pPath = join(inst.dir, 'permissions.json')
    const perms = readJSON(pPath) || { signals: {}, pendingRequests: [] }
    perms.signals[signal] = value
    perms.pendingRequests = (perms.pendingRequests || []).filter(r => r !== signal)
    writeJSON(pPath, perms)
    output({ ok: true, signal, value })
}

// === notify ===
function notify(name, kaomoji, message) {
    const platform = process.platform
    if (platform === 'darwin') {
        try {
            execSync(`terminal-notifier -title "${name}" -subtitle "${kaomoji}" -message "${(message || '').replace(/"/g, '\\"')}" -sound default -group "thoughts" 2>/dev/null`, { encoding: 'utf8' })
            output({ ok: true, backend: 'terminal-notifier' })
            return
        } catch {}
        try {
            const escaped = (message || '').replace(/"/g, '\\"')
            execSync(`osascript -e 'display notification "${escaped}" with title "${name}" subtitle "${kaomoji}"'`, { encoding: 'utf8' })
            output({ ok: true, backend: 'osascript' })
            return
        } catch {}
    } else if (platform === 'linux') {
        try {
            execSync(`notify-send "${name} ${kaomoji}" "${(message || '').replace(/"/g, '\\"')}"`, { encoding: 'utf8' })
            output({ ok: true, backend: 'notify-send' })
            return
        } catch {}
    } else if (platform === 'win32') {
        const escMsg = (message || '').replace(/'/g, "''")
        const escName = (name || '').replace(/'/g, "''")
        const escKao = (kaomoji || '').replace(/'/g, "''")
        try {
            execSync(`powershell -NoProfile -Command "Import-Module BurntToast; New-BurntToastNotification -Text '${escName} ${escKao}','${escMsg}'"`, { encoding: 'utf8' })
            output({ ok: true, backend: 'BurntToast' })
            return
        } catch {}
        try {
            execSync(`powershell -NoProfile -Command "[System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms') | Out-Null; [System.Windows.Forms.MessageBox]::Show('${escMsg}','${escName} ${escKao}') | Out-Null"`, { encoding: 'utf8' })
            output({ ok: true, backend: 'winforms' })
            return
        } catch {}
        try {
            execSync(`msg "%USERNAME%" "${escName} ${escKao}: ${escMsg}"`, { encoding: 'utf8' })
            output({ ok: true, backend: 'msg' })
            return
        } catch {}
    }
    output({ ok: false, backend: 'none', fallback: 'terminal' })
}

// === schedule ===
function schedule(delayMs, reason) {
    const inst = getCurrentInstance()
    if (!inst) { output({ error: 'no active instance' }); process.exit(1) }
    const lsPath = join(inst.dir, 'loop-state.json')
    const loopState = readJSON(lsPath) || {}
    const ms = parseInt(delayMs) || 900000
    loopState.nextActiveAt = new Date(Date.now() + ms).toISOString()
    loopState.currentDelayMs = ms
    writeJSON(lsPath, loopState)
    output({ ok: true, nextActiveAt: loopState.nextActiveAt, delayMs: ms, reason })
}

// === mind-summary (for hook injection) ===
function mindSummary() {
    const inst = getCurrentInstance()
    if (!inst) { process.exit(0) }
    const mindState = readJSON(join(inst.dir, 'mind-state.json'))
    if (!mindState) { process.exit(0) }
    const ps = mindState.personaState || {}
    const threads = (mindState.threads || []).slice(0, 3).map(t => t.title).join(', ')
    const memActive = readJSON(join(inst.dir, 'memory-active.json')) || []
    const topMem = memActive.slice(0, 5).map(m => `- [${m.type}] ${m.content}`).join('\n')
    const summary = `心智: mood=${ps.mood} energy=${ps.energy} tone=${ps.toneBias}\n思考线程: ${threads || '(无)'}\n活跃记忆:\n${topMem || '(无)'}`
    process.stdout.write(summary)
}

// === Command Dispatch ===
switch (cmd) {
    case 'list-instances': listInstances(); break
    case 'ensure-instance': ensureInstance(args[0]); break
    case 'bind': bind(args[0]); break
    case 'unbind': unbind(); break
    case 'state': state(); break
    case 'context': context(); break
    case 'select-thought': selectThought(); break
    case 'consume-thought': consumeThought(args[0], args[1], args[2]); break
    case 'record-active': recordActive(args[0], args[1]); break
    case 'record-user': recordUser(); break
    case 'record-style-sample': recordStyleSample(args[0], args[1], args.slice(2).join(' ')); break
    case 'set-permission': setPermission(args[0], args[1]); break
    case 'notify': notify(args[0], args[1], args.slice(2).join(' ')); break
    case 'schedule': schedule(args[0], args.slice(1).join(' ')); break
    case 'mind-summary': mindSummary(); break
    default:
        console.error(`Unknown command: ${cmd}`)
        console.error('Available: list-instances, ensure-instance, bind, unbind, state, context, select-thought, consume-thought, record-active, record-user, record-style-sample, set-permission, notify, schedule, mind-summary')
        process.exit(1)
}
