// 给控制台页面用的受控通道。
//
// 界面是网关吐出来的 http 页面，不能直接 require('electron')；这里只把内嵌工作台
// 需要的那几个动作挑出来挂到 window.gpool 上，其它一律不暴露。
// 页面若在普通浏览器里打开（window.gpool 不存在），工作台会自动降级到外部窗口。
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('gpool', {
  available: true,
  browser: {
    open: a => ipcRenderer.invoke('gpool.browser.open', a),
    close: id => ipcRenderer.invoke('gpool.browser.close', id),
    activate: id => ipcRenderer.invoke('gpool.browser.activate', id),
    reload: id => ipcRenderer.invoke('gpool.browser.reload', id),
    navigate: dir => ipcRenderer.invoke('gpool.browser.navigate', dir),
    navigateTo: url => ipcRenderer.invoke('gpool.browser.navigateTo', url),
    setLayout: o => ipcRenderer.invoke('gpool.browser.setLayout', o),
    snapshot: () => ipcRenderer.invoke('gpool.browser.snapshot'),
    syncCookies: id => ipcRenderer.invoke('gpool.browser.syncCookies', id),
  },
  // 托盘精简后「重启网关」不再有入口，改端口这类必须重启的操作从界面里触发
  restartGateway: () => ipcRenderer.invoke('gpool.gateway.restart'),
  // 主进程 → 页面：标签状态变化（标题、地址、加载中、错误…）
  onBrowserEvent: cb => {
    const handler = (_e, snap) => { try { cb(snap); } catch (err) { /* 页面自行处理 */ } };
    ipcRenderer.on('gpool.browser.event', handler);
    return () => ipcRenderer.removeListener('gpool.browser.event', handler);
  },
});
