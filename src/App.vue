<script setup lang="ts">
import { ref, reactive, onMounted, onUnmounted, nextTick } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import { listen } from '@tauri-apps/api/event'
import OnboardingView from './components/OnboardingView.vue'
import ProfileModal from './components/ProfileModal.vue'

// === 类型定义 ===

interface AppStatus {
    gateway: { status: string } | string
    scheduler: string
    lastError: string | null
    lastPollTime: string | null
    lastUserMessageTime: string | null
    appPhase: 'Onboarding' | 'Active'
}

interface ChatItem {
    type: 'user' | 'ai' | 'auto-reminder' | 'system-error'
    content: string
    timestamp: string
}

// === 状态 ===

const inputMessage = ref('')
const isSending = ref(false)
const chatList = reactive<ChatItem[]>([])
const chatContainer = ref<HTMLElement | null>(null)
/** 画像弹窗是否打开 */
const showProfileModal = ref(false)
const profileModalRef = ref<InstanceType<typeof ProfileModal> | null>(null)

const appStatus = reactive<AppStatus>({
    gateway: 'Stopped',
    scheduler: 'Idle',
    lastError: null,
    lastPollTime: null,
    lastUserMessageTime: null,
    appPhase: 'Active',
})

// === 工具函数 ===

/** 获取当前时间字符串 */
function now(): string {
    return new Date().toLocaleTimeString('zh-CN', { hour12: false })
}

/** 解析 Gateway 状态为显示文本 */
function gatewayLabel(gw: AppStatus['gateway']): string {
    if (typeof gw === 'string') {
        switch (gw) {
            case 'Ready': return '就绪'
            case 'Starting': return '启动中...'
            case 'Stopped': return '已停止'
            default: return gw
        }
    }
    if ('Error' in gw) return '错误'
    return '未知'
}

/** 解析 Gateway 状态颜色 */
function gatewayColor(gw: AppStatus['gateway']): string {
    const label = gatewayLabel(gw)
    if (label === '就绪') return 'text-green-500'
    if (label === '启动中...') return 'text-yellow-500'
    if (label.includes('错误')) return 'text-red-500'
    return 'text-gray-400'
}

/** 滚动到底部 */
async function scrollToBottom() {
    await nextTick()
    if (chatContainer.value) {
        chatContainer.value.scrollTop = chatContainer.value.scrollHeight
    }
}

// === 核心逻辑 ===

/** 发送用户消息 */
async function sendMessage() {
    const msg = inputMessage.value.trim()
    if (!msg || isSending.value) return

    inputMessage.value = ''
    isSending.value = true

    chatList.push({ type: 'user', content: msg, timestamp: now() })
    await scrollToBottom()

    try {
        const reply = await invoke<string>('send_user_message', { message: msg })
        chatList.push({ type: 'ai', content: reply, timestamp: now() })
    } catch (e) {
        chatList.push({
            type: 'system-error',
            content: String(e),
            timestamp: now(),
        })
    } finally {
        isSending.value = false
        await scrollToBottom()
        refreshStatus()
    }
}

/** 手动触发检查 */
async function triggerManualPoll() {
    try {
        const result = await invoke<string | null>('trigger_manual_poll')
        if (result) {
            chatList.push({ type: 'auto-reminder', content: result, timestamp: now() })
            await scrollToBottom()
        }
    } catch (e) {
        chatList.push({
            type: 'system-error',
            content: `手动检查失败: ${String(e)}`,
            timestamp: now(),
        })
        await scrollToBottom()
    }
}

/** 刷新状态 */
async function refreshStatus() {
    try {
        const status = await invoke<AppStatus>('get_status')
        Object.assign(appStatus, status)
    } catch (_) {
        // 静默处理
    }
}

/** 重启 Gateway */
async function handleRestartGateway() {
    try {
        await invoke('restart_gateway')
        await refreshStatus()
    } catch (e) {
        chatList.push({
            type: 'system-error',
            content: `重启 Gateway 失败: ${String(e)}`,
            timestamp: now(),
        })
        await scrollToBottom()
    }
}

/** Onboarding 完成回调 */
function handleOnboardingComplete() {
    refreshStatus()
}

// === 生命周期 ===

let unlistenReminder: (() => void) | null = null
let unlistenStatus: (() => void) | null = null
let unlistenOnboardingComplete: (() => void) | null = null
let unlistenProfileUpdated: (() => void) | null = null
let statusTimer: ReturnType<typeof setInterval> | null = null

onMounted(async () => {
    // 监听自动提醒事件
    unlistenReminder = await listen<string>('auto-reminder', (event) => {
        chatList.push({
            type: 'auto-reminder',
            content: event.payload,
            timestamp: now(),
        })
        scrollToBottom()
    })

    // 监听状态变更事件
    unlistenStatus = await listen('status-changed', () => {
        refreshStatus()
    })

    // 监听 onboarding 完成事件
    unlistenOnboardingComplete = await listen('onboarding-complete', () => {
        refreshStatus()
    })

    // 监听画像更新事件
    unlistenProfileUpdated = await listen('profile-updated', () => {
        if (showProfileModal.value && profileModalRef.value) {
            profileModalRef.value.refresh()
        }
    })

    // 初次获取状态
    await refreshStatus()

    // 定时刷新状态（每 30 秒）
    statusTimer = setInterval(refreshStatus, 30000)
})

onUnmounted(() => {
    unlistenReminder?.()
    unlistenStatus?.()
    unlistenOnboardingComplete?.()
    unlistenProfileUpdated?.()
    if (statusTimer) clearInterval(statusTimer)
})
</script>

<template>
    <!-- Onboarding 模式 -->
    <OnboardingView
        v-if="appStatus.appPhase === 'Onboarding'"
        @complete="handleOnboardingComplete"
    />

    <!-- 正常模式 -->
    <div v-else data-alt="app-root" class="h-screen flex flex-col bg-gray-900 text-gray-100">
        <!-- 顶部状态栏 -->
        <header data-alt="status-bar" class="flex items-center justify-between px-4 py-2 bg-gray-800 border-b border-gray-700 text-sm shrink-0">
            <div class="flex items-center gap-4">
                <span class="font-bold text-base">思绪</span>
                <span class="flex items-center gap-1">
                    <span class="inline-block w-2 h-2 rounded-full" :class="gatewayLabel(appStatus.gateway) === '就绪' ? 'bg-green-500' : gatewayLabel(appStatus.gateway) === '启动中...' ? 'bg-yellow-500' : 'bg-red-500'" />
                    <span :class="gatewayColor(appStatus.gateway)">Gateway: {{ gatewayLabel(appStatus.gateway) }}</span>
                </span>
                <span class="text-gray-400">轮询: {{ appStatus.scheduler }}</span>
                <span v-if="appStatus.lastPollTime" class="text-gray-500">上次: {{ appStatus.lastPollTime }}</span>
            </div>
            <div class="flex items-center gap-2">
                <button
                    data-alt="profile-btn"
                    class="px-2 py-1 text-xs rounded bg-gray-700 hover:bg-gray-600 transition"
                    title="查看我的画像"
                    @click="showProfileModal = true"
                >
                    画像
                </button>
                <button
                    data-alt="restart-gateway-btn"
                    class="px-2 py-1 text-xs rounded bg-gray-700 hover:bg-gray-600 transition"
                    @click="handleRestartGateway"
                >
                    重启 Gateway
                </button>
            </div>
        </header>

        <!-- 错误提示 -->
        <div v-if="appStatus.lastError" data-alt="error-banner" class="px-4 py-2 bg-red-900/50 text-red-300 text-sm shrink-0">
            {{ appStatus.lastError }}
        </div>

        <!-- 中部消息区 -->
        <main
            ref="chatContainer"
            data-alt="chat-area"
            class="flex-1 overflow-y-auto px-4 py-4 space-y-3"
        >
            <!-- 空状态 -->
            <div v-if="chatList.length === 0" class="flex items-center justify-center h-full text-gray-500">
                <p>发送一条消息开始对话，或等待自动提醒</p>
            </div>

            <!-- 消息列表 -->
            <div
                v-for="(item, index) in chatList"
                :key="index"
                :data-alt="`chat-item-${item.type}`"
                class="max-w-3xl"
                :class="item.type === 'user' ? 'ml-auto' : ''"
            >
                <!-- 用户消息 -->
                <div v-if="item.type === 'user'" class="flex justify-end">
                    <div class="bg-blue-600 text-white px-4 py-2 rounded-2xl rounded-br-sm max-w-lg break-words">
                        <p class="whitespace-pre-wrap">{{ item.content }}</p>
                        <span class="text-xs text-blue-200 mt-1 block text-right">{{ item.timestamp }}</span>
                    </div>
                </div>

                <!-- AI 回复 -->
                <div v-else-if="item.type === 'ai'" class="flex justify-start">
                    <div class="bg-gray-700 px-4 py-2 rounded-2xl rounded-bl-sm max-w-lg break-words">
                        <p class="whitespace-pre-wrap">{{ item.content }}</p>
                        <span class="text-xs text-gray-400 mt-1 block">{{ item.timestamp }}</span>
                    </div>
                </div>

                <!-- 自动提醒 -->
                <div v-else-if="item.type === 'auto-reminder'" class="flex justify-start">
                    <div class="bg-amber-900/40 border border-amber-700/50 px-4 py-2 rounded-2xl max-w-lg break-words">
                        <div class="flex items-center gap-1 mb-1">
                            <span class="text-xs text-amber-400 font-medium">自动提醒</span>
                        </div>
                        <p class="whitespace-pre-wrap text-amber-100">{{ item.content }}</p>
                        <span class="text-xs text-amber-500 mt-1 block">{{ item.timestamp }}</span>
                    </div>
                </div>

                <!-- 系统错误 -->
                <div v-else-if="item.type === 'system-error'" class="flex justify-center">
                    <div class="bg-red-900/30 border border-red-700/50 px-4 py-2 rounded-lg max-w-lg text-sm text-red-300 break-words">
                        <p class="whitespace-pre-wrap">{{ item.content }}</p>
                        <span class="text-xs text-red-500 mt-1 block">{{ item.timestamp }}</span>
                    </div>
                </div>
            </div>

            <!-- 发送中指示器 -->
            <div v-if="isSending" class="flex justify-start">
                <div class="bg-gray-700 px-4 py-3 rounded-2xl rounded-bl-sm">
                    <div class="flex items-center gap-1">
                        <span class="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style="animation-delay: 0ms" />
                        <span class="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style="animation-delay: 150ms" />
                        <span class="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style="animation-delay: 300ms" />
                    </div>
                </div>
            </div>
        </main>

        <!-- 底部输入区 -->
        <footer data-alt="input-area" class="shrink-0 border-t border-gray-700 bg-gray-800 px-4 py-3">
            <form class="flex items-center gap-2" @submit.prevent="sendMessage">
                <input
                    v-model="inputMessage"
                    data-alt="message-input"
                    type="text"
                    placeholder="输入消息..."
                    class="flex-1 bg-gray-700 text-gray-100 px-4 py-2 rounded-lg outline-none placeholder-gray-500 focus:ring-2 focus:ring-blue-500/50 transition"
                    :disabled="isSending"
                />
                <button
                    data-alt="send-btn"
                    type="submit"
                    class="px-4 py-2 bg-blue-600 hover:bg-blue-500 disabled:bg-gray-600 disabled:cursor-not-allowed text-white rounded-lg transition font-medium"
                    :disabled="isSending || !inputMessage.trim()"
                >
                    发送
                </button>
                <button
                    data-alt="manual-poll-btn"
                    type="button"
                    class="px-3 py-2 bg-gray-700 hover:bg-gray-600 text-gray-300 rounded-lg transition text-sm"
                    @click="triggerManualPoll"
                    :disabled="isSending"
                >
                    手动检查
                </button>
            </form>
        </footer>

        <!-- 画像弹窗 -->
        <ProfileModal
            v-if="showProfileModal"
            ref="profileModalRef"
            @close="showProfileModal = false"
        />
    </div>
</template>
