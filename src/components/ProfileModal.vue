<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { invoke } from '@tauri-apps/api/core'

// === 事件 ===

const emit = defineEmits<{
    close: []
}>()

// === 状态 ===

const profile = ref<Record<string, unknown> | null>(null)
const loading = ref(true)

// === 工具函数 ===

/** 将画像值格式化为展示文本 */
function formatValue(value: unknown): string {
    if (Array.isArray(value)) {
        return value.join('、')
    }
    if (value === null || value === undefined) {
        return '-'
    }
    return String(value)
}

/** 将 camelCase 字段名转为可读的中文标签 */
function formatLabel(key: string): string {
    const labelMap: Record<string, string> = {
        nickname: '昵称',
        gender: '性别',
        age: '年龄',
        ageRange: '年龄段',
        occupation: '职业',
        hobbies: '爱好',
        interests: '感兴趣的',
        dislikes: '不喜欢的',
        habits: '日常习惯',
        location: '所在地',
    }
    return labelMap[key] || key
}

// === 生命周期 ===

onMounted(async () => {
    await loadProfile()
})

/** 加载画像数据 */
async function loadProfile() {
    loading.value = true
    try {
        const data = await invoke<Record<string, unknown> | null>('get_user_profile')
        profile.value = data
    } catch (_) {
        profile.value = null
    } finally {
        loading.value = false
    }
}

/** 暴露 refresh 方法供父组件调用 */
defineExpose({ refresh: loadProfile })
</script>

<template>
    <div data-alt="profile-modal-overlay" class="fixed inset-0 z-50 flex items-center justify-center bg-black/60" @click.self="emit('close')">
        <div data-alt="profile-modal" class="bg-gray-800 rounded-xl border border-gray-700 w-full max-w-md mx-4 overflow-hidden shadow-2xl">
            <!-- 标题栏 -->
            <div class="flex items-center justify-between px-5 py-3 border-b border-gray-700">
                <h3 class="font-medium text-gray-100">我的画像</h3>
                <button
                    data-alt="profile-modal-close"
                    class="text-gray-400 hover:text-gray-200 transition text-lg leading-none"
                    @click="emit('close')"
                >
                    &times;
                </button>
            </div>

            <!-- 内容 -->
            <div class="px-5 py-4 max-h-96 overflow-y-auto">
                <!-- 加载中 -->
                <div v-if="loading" class="text-center text-gray-500 py-8">
                    加载中...
                </div>

                <!-- 无画像 -->
                <div v-else-if="!profile || Object.keys(profile).length === 0" class="text-center text-gray-500 py-8">
                    <p>暂无画像数据</p>
                    <p class="text-xs mt-1">完成 onboarding 对话后将在此展示</p>
                </div>

                <!-- 画像字段列表 -->
                <div v-else class="space-y-3">
                    <div
                        v-for="(value, key) in profile"
                        :key="String(key)"
                        data-alt="profile-field"
                        class="flex items-start gap-3"
                    >
                        <span class="text-sm text-gray-400 min-w-20 shrink-0 pt-0.5">{{ formatLabel(String(key)) }}</span>
                        <span class="text-sm text-gray-100 break-words">{{ formatValue(value) }}</span>
                    </div>
                </div>
            </div>
        </div>
    </div>
</template>
