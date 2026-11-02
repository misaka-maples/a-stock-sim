# 长期运行配置

本目录 service 针对 openarmx@172.168.200.241 的 /home/openarmx/test；迁移前调整 User、Group、WorkingDirectory、ExecStart 与环境变量路径。

- systemd 自动启动及崩溃重启，10秒重试；5分钟连续10次启动失败后停止重试，避免故障循环。
- MemoryHigh=8G 是回收压力阈值，MemoryMax=10G 是整个服务进程组硬上限，超过可能触发 OOM。它不是回测内存保证。
- 回测 GET/POST 共用非排队锁，忙时409，主机/服务组可用内存不足6GiB时503。一次一个回测，不能部署多个uvicorn worker。准入不能预测任意规模任务的最终内存峰值。
- 回测退出后GC，并在本机启用glibc malloc_trim；它只能尝试释放空闲堆页，不保证RSS降低。
- logs/server.log 每卷50MiB、5个备份（当前卷加备份约300MiB），关闭HTTP access日志但保留业务INFO与错误。第三方stdout/stderr进入独立journal，100MiB/14天预算。
- 周期估值检查点30秒；成交/委托、收盘快照前和退出仍立即持久化。异常断电可能丢失最后30秒非成交估值和周期策略状态。
- 90天前的标准日终快照压缩为gzip，原ID仍可读取/恢复/导出；手动及恢复前备份不参与轮转。历史永久保留，压缩并非总磁盘硬配额。
- trades/orders保留全量，避免改变累计收益和风控；彻底消除长期线性成本需后续迁移至可增量写入的交易账本。

## 安装（先备份、测试并优雅停止旧进程）

```sh
sudo install -m 644 deploy/openarmx-quant.service /etc/systemd/system/openarmx-quant.service
sudo install -d /etc/systemd/journald@openarmx-quant.conf.d
sudo install -m 644 deploy/journald-openarmx-quant.conf /etc/systemd/journald@openarmx-quant.conf.d/limits.conf
sudo systemctl daemon-reload
sudo systemctl enable --now openarmx-quant.service
sudo systemctl status openarmx-quant.service
```

已接管后 start.sh/stop.sh/status.sh 使用对应systemd服务。查看独立系统日志：

```sh
sudo journalctl --namespace=openarmx-quant -u openarmx-quant.service -n 50
```

/health 包含RSS、主机可用内存、cgroup余量、回测准入与运行状态。systemd负责进程退出/OOM后的拉起；当前未配置HTTP健康失败自动重启，避免把网络/数据源故障转为重启循环。

回退：停止并禁用service，恢复备份内的代码（保留最新账户与历史数据），daemon-reload后使用旧start.sh启动。除明确需要恢复账户状态外，不覆盖现行账户文件。旧代码不支持读取压缩日终快照，回退前须将新增的.json.gz无损解压并更新快照索引；完整历史在备份中保留。
