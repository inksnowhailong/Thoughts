<script setup lang="ts">
import { ref, reactive, nextTick } from 'vue'
import { invoke } from '@tauri-apps/api/core'

// === 类型 ===

interface ChatItem {
    type: 'user' | 'ai' | 'system-error'
    content: string
    timestamp: string
}

// === 事件 ===

const emit = defineEmits<{
    /** onboarding 完成或跳过后触发 */
    complete: []
}>()

// === 状态 ===

const inputMessage = ref('')
const isSending = ref(false)
const chatList = reactive<ChatItem[]>([])
const chatContainer = ref<HTMLElement | null>(null)
/** 是否已发送过第一条消息（用于触发 AI 首次问候） */
const hasStarted = ref(false)

// === 工具函数 ===

/** 获取当前时间字符串 */
function now(): string {
    return new Date().toLocaleTimeString('zh-CN', { hour12: false })
}

/** 滚动到底部 */
async function scrollToBottom() {
    await nextTick()
    if (chatContainer.value) {
        chatContainer.value.scrollTop = chatContainer.value.scrollHeight
    }
}

// === 核心逻辑 ===

/** 发送 onboarding 消息 */
async function sendMessage() {
    const msg = inputMessage.value.trim()
    if (!msg || isSending.value) return

    inputMessage.value = ''
    isSending.value = true
    hasStarted.value = true

    chatList.push({ type: 'user', content: msg, timestamp: now() })
    await scrollToBottom()

    try {
        const reply = await invoke<string>('send_onboarding_message', { message: msg })
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
    }
}

/** 跳过 onboarding */
async function skipOnboarding() {
    try {
        await invoke('skip_onboarding')
        emit('complete')
    } catch (e) {
        chatList.push({
            type: 'system-error',
            content: `跳过失败: ${String(e)}`,
            timestamp: now(),
        })
        await scrollToBottom()
    }
}
</script>

<template>
    <div data-alt="onboarding-view" class="h-screen flex flex-col bg-gray-900 text-gray-100">
        <!-- 顶部标题栏 -->
        <header data-alt="onboarding-header" class="flex items-center justify-between px-4 py-3 bg-gray-800 border-b border-gray-700 shrink-0">
            <div class="flex items-center gap-2">
                <span class="font-bold text-base">思绪</span>
                <span class="text-sm text-purple-400">初次见面</span>
            </div>
            <button
                data-alt="skip-onboarding-btn"
                class="px-3 py-1 text-xs rounded bg-gray-700 hover:bg-gray-600 text-gray-400 hover:text-gray-200 transition"
                @click="skipOnboarding"
            >
                跳过设定
            </button>
        </header>

        <!-- 消息区 -->
        <main
            ref="chatContainer"
            data-alt="onboarding-chat-area"
            class="flex-1 overflow-y-auto px-4 py-4 space-y-3"
        >
            <!-- 欢迎提示（未开始对话时显示） -->
            <div v-if="!hasStarted" class="flex flex-col items-center justify-center h-full text-center space-y-4">
                <div class="text-4xl">👋</div>
                <h2 class="text-xl font-medium text-gray-200">你好！我是思绪</h2>
                <p class="text-gray-400 max-w-sm">让我先了解一下你吧～随便聊聊你的兴趣、爱好，这样以后我能给你更有针对性的内容</p>
                <p class="text-xs text-gray-500">发送任意消息开始</p>
            </div>

            <!-- 消息列表 -->
            <div
                v-for="(item, index) in chatList"
                :key="index"
                :data-alt="`onboarding-chat-${item.type}`"
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
                    <div class="bg-purple-900/40 border border-purple-700/30 px-4 py-2 rounded-2xl rounded-bl-sm max-w-lg break-words">
                        <p class="whitespace-pre-wrap text-purple-100">{{ item.content }}</p>
                        <span class="text-xs text-purple-400 mt-1 block">{{ item.timestamp }}</span>
                    </div>
                </div>

                <!-- 系统错误 -->
                <div v-else-if="item.type === 'system-error'" class="flex justify-center">
                    <div class="bg-red-900/30 border border-red-700/50 px-4 py-2 rounded-lg max-w-lg text-sm text-red-300 break-words">
                        <p class="whitespace-pre-wrap">{{ item.content }}</p>
                    </div>
                </div>
            </div>

            <!-- 发送中指示器 -->
            <div v-if="isSending" class="flex justify-start">
                <div class="bg-purple-900/40 border border-purple-700/30 px-4 py-3 rounded-2xl rounded-bl-sm">
                    <div class="flex items-center gap-1">
                        <span class="w-2 h-2 bg-purple-400 rounded-full animate-bounce" style="animation-delay: 0ms" />
                        <span class="w-2 h-2 bg-purple-400 rounded-full animate-bounce" style="animation-delay: 150ms" />
                        <span class="w-2 h-2 bg-purple-400 rounded-full animate-bounce" style="animation-delay: 300ms" />
                    </div>
                </div>
            </div>
        </main>

        <!-- 输入区 -->
        <footer data-alt="onboarding-input-area" class="shrink-0 border-t border-gray-700 bg-gray-800 px-4 py-3">
            <form class="flex items-center gap-2" @submit.prevent="sendMessage">
                <input
                    v-model="inputMessage"
                    data-alt="onboarding-input"
                    type="text"
                    placeholder="随便聊聊..."
                    class="flex-1 bg-gray-700 text-gray-100 px-4 py-2 rounded-lg outline-none placeholder-gray-500 focus:ring-2 focus:ring-purple-500/50 transition"
                    :disabled="isSending"
                />
                <button
                    data-alt="onboarding-send-btn"
                    type="submit"
                    class="px-4 py-2 bg-purple-600 hover:bg-purple-500 disabled:bg-gray-600 disabled:cursor-not-allowed text-white rounded-lg transition font-medium"
                    :disabled="isSending || !inputMessage.trim()"
                >
                    发送
                </button>
            </form>
        </footer>
    </div>
</template>
