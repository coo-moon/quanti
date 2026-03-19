<template>
  <div id="app">
    <nav class="nav-bar">
      <router-link to="/" class="nav-brand">Quanti</router-link>
      <div class="nav-links">
        <router-link to="/">仪表盘</router-link>
        <router-link to="/backtest">回测中心</router-link>
      </div>
    </nav>
    <main>
      <router-view />
    </main>
  </div>
</template>

<style>
* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: #fff;
  color: #333;
}
.nav-bar {
  display: flex;
  align-items: center;
  padding: 0 20px;
  height: 48px;
  background: #001529;
  color: white;
}
.nav-brand {
  font-size: 18px;
  font-weight: bold;
  color: white;
  text-decoration: none;
  margin-right: 32px;
}
.nav-links a {
  color: rgba(255, 255, 255, 0.65);
  text-decoration: none;
  margin-right: 20px;
  font-size: 14px;
}
.nav-links a.router-link-active {
  color: white;
}
main {
  max-width: 1200px;
  margin: 0 auto;
}
</style>
