import { createRouter, createWebHistory } from "vue-router";
import Dashboard from "../views/Dashboard.vue";
import Backtest from "../views/Backtest.vue";
import Screener from "../views/Screener.vue";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", name: "dashboard", component: Dashboard },
    { path: "/backtest", name: "backtest", component: Backtest },
    { path: "/screener", name: "screener", component: Screener },
  ],
});

export default router;
