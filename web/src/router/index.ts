import { createRouter, createWebHistory } from "vue-router";
import Dashboard from "../views/Dashboard.vue";
import Agent from "../views/Agent.vue";
import Screener from "../views/Screener.vue";
import Pool from "../views/Pool.vue";

// Heavy route loaded on demand — Backtest pulls in ECharts (~500 KB).
const Backtest = () => import("../views/Backtest.vue");
const RiskAudit = () => import("../views/RiskAudit.vue");
// EtfGrid also pulls in ECharts — lazy-load.
const EtfGrid = () => import("../views/EtfGrid.vue");

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", name: "dashboard", component: Dashboard },
    { path: "/agent", name: "agent", component: Agent },
    { path: "/backtest", name: "backtest", component: Backtest },
    { path: "/screener", name: "screener", component: Screener },
    { path: "/pool", name: "pool", component: Pool },
    { path: "/etf-grid", name: "etf-grid", component: EtfGrid },
    { path: "/risk", name: "risk", component: RiskAudit },
  ],
});

export default router;
