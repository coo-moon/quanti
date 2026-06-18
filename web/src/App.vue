<template>
  <div id="app">
    <nav class="nav-bar">
      <div class="nav-inner">
        <router-link to="/" class="nav-brand">
          <span class="brand-icon">Q</span>
          <span class="brand-text">Quanti</span>
        </router-link>
        <div class="nav-links">
          <router-link to="/" class="nav-link">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
              <rect x="1" y="1" width="6" height="6" rx="1.5" stroke="currentColor" stroke-width="1.5" />
              <rect x="9" y="1" width="6" height="6" rx="1.5" stroke="currentColor" stroke-width="1.5" />
              <rect x="1" y="9" width="6" height="6" rx="1.5" stroke="currentColor" stroke-width="1.5" />
              <rect x="9" y="9" width="6" height="6" rx="1.5" stroke="currentColor" stroke-width="1.5" />
            </svg>
            仪表盘
          </router-link>
          <router-link to="/agent" class="nav-link">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
              <circle cx="8" cy="6" r="3" stroke="currentColor" stroke-width="1.5" />
              <path d="M2 14c0-3 2.5-5 6-5s6 2 6 5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
            </svg>
            AI Agent
          </router-link>
          <router-link to="/screener" class="nav-link">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
              <circle cx="7" cy="7" r="5.5" stroke="currentColor" stroke-width="1.5" />
              <path d="M11 11l3.5 3.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
            </svg>
            选股中心
          </router-link>
          <router-link to="/backtest" class="nav-link">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
              <polyline points="1,12 5,6 9,9 15,3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" />
              <polyline points="11,3 15,3 15,7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
            回测中心
          </router-link>
          <router-link to="/pool" class="nav-link">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
              <rect x="1" y="3" width="14" height="10" rx="2" stroke="currentColor" stroke-width="1.5" />
              <path d="M4 3V2a1 1 0 011-1h6a1 1 0 011 1v1" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
              <path d="M1 7h14" stroke="currentColor" stroke-width="1.5" />
            </svg>
            股票池
          </router-link>
        </div>
        <span class="account-badge" :class="isLive ? 'acct-live' : 'acct-paper'"
              :title="isLive ? '实盘 — 真实资金,下单会真实成交' : '模拟盘 — 不涉及真实资金'">
          {{ isLive ? "● 实盘" : "模拟盘" }}
        </span>
      </div>
    </nav>
    <main>
      <router-view />
    </main>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue";
import { fetchMeta } from "./api/client";

// Paper vs live, surfaced as an always-visible nav badge so real money is
// never mistaken for the simulator. Defaults to paper if /meta is unreachable.
const isLive = ref(false);
onMounted(async () => {
  try {
    isLive.value = (await fetchMeta()).data.is_live;
  } catch {
    isLive.value = false;
  }
});
</script>

<style>
@import "./assets/base.css";

#app {
  min-height: 100vh;
}

.nav-bar {
  position: sticky;
  top: 0;
  z-index: 100;
  background: rgba(255, 255, 255, 0.72);
  backdrop-filter: saturate(180%) blur(20px);
  -webkit-backdrop-filter: saturate(180%) blur(20px);
  border-bottom: 0.5px solid rgba(0, 0, 0, 0.1);
}

.nav-inner {
  max-width: 1120px;
  margin: 0 auto;
  padding: 0 24px;
  height: 52px;
  display: flex;
  align-items: center;
  gap: 8px;
}

.nav-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  text-decoration: none;
  margin-right: 24px;
}

.brand-icon {
  width: 30px;
  height: 30px;
  background: linear-gradient(135deg, #0071e3, #40a9ff);
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: white;
  font-weight: 700;
  font-size: 16px;
  letter-spacing: -0.5px;
}

.brand-text {
  font-size: 18px;
  font-weight: 600;
  color: var(--color-text-primary);
  letter-spacing: -0.3px;
}

.nav-links {
  display: flex;
  gap: 4px;
}

/* Paper/live indicator — pushed to the far right of the nav. Live is loud
   (red, filled) so real money can never be mistaken for the simulator. */
.account-badge {
  margin-left: auto;
  padding: 4px 12px;
  border-radius: 999px;
  font-size: 13px;
  font-weight: 600;
  white-space: nowrap;
}
.acct-paper {
  color: var(--color-text-secondary);
  background: rgba(0, 0, 0, 0.06);
}
.acct-live {
  color: #fff;
  background: #e02424;
  box-shadow: 0 0 0 3px rgba(224, 36, 36, 0.18);
}

.nav-link {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  border-radius: 8px;
  font-size: 14px;
  font-weight: 500;
  color: var(--color-text-secondary);
  text-decoration: none;
  transition: all var(--transition);
}

.nav-link:hover {
  color: var(--color-text-primary);
  background: rgba(0, 0, 0, 0.04);
}

.nav-link.router-link-exact-active {
  color: var(--color-accent);
  background: var(--color-blue-bg);
}

.nav-link svg {
  flex-shrink: 0;
}

main {
  max-width: 1120px;
  margin: 0 auto;
  padding: 32px 24px 64px;
}
</style>
