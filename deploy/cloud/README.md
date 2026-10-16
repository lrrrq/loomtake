# Loomtake 云端部署门禁

本目录只包含 source-only 容器和运维契约，不包含密钥、真实数据包、Brief 或运行产物。

构建时必须传入完整源提交和 runner 版本，发布后运行记录固化实际镜像 digest：

```bash
docker build -f deploy/cloud/Dockerfile \
  --build-arg MPLUS_SOURCE_COMMIT=<40位源SHA> \
  --build-arg MPLUS_RUNNER_VERSION=1.1.1 \
  -t mplus-runner:1.1.1-<short-sha> .
```

启动 API 或 Worker 前先由管理员显式迁移；运行进程只检查 Schema，不自动建表：

```bash
./mplus admin cloud migrate --check
./mplus admin cloud migrate --apply
./mplus admin package sync --configured --cloud
./mplus admin package sync --configured --formal --cloud
```

第一条包命令只注册并校验，不移动 `formal`；第二条只在六包全部通过后于一个事务内提升。

公网启动必须同时配置 HTTPS 公网 URL、GitHub OAuth、账号或组织允许名单、PostgreSQL 和私有 COS。任一项缺失时服务拒绝外网监听。密钥只通过云密钥服务注入，不写入镜像、仓库或命令行。

云端视觉能力固定使用镜像内的 `mmx-cli 1.0.16`。Worker 必须从云密钥服务注入 `MINIMAX_API_KEY`；本机 `mmx auth login` 的登录态只供本地运行使用，不复制进容器。缺少命令或 Key 时视觉能力必须标记为不可用，不得静默切换模型。

`retention-policy.json` 是需要在 COS、数据库备份和监控平台中实际落地的验收基线；文件存在不等于云策略已生效。


## 恢复目标与证据

沿用需求 NFR-03：最多丢失最近 15 分钟的新操作，故障确认后 2 小时内恢复服务。`retention-policy.json` 的 `database.rpo_hours` / `rto_hours` 单位均为小时，分别为 `0.25` / `2`；读取方必须按数值处理，不能截断为整数。当前仓库没有读取这两个字段的运行时代码；云平台配置和监控接入时需要核对单位及实际生效值。项目数据目录迁移的 RPO=0、程序/配置回切 RTO≤60 分钟属于另一个恢复场景，不能混用或替代本指标。

一次可恢复集合必须对应同一恢复点，包含数据库一致快照及所需日志、冻结输入和 blob、不可变资产包及注册绑定、运行与交付 artifact、必要非密钥配置、Schema 和应用候选身份。源码 bundle 本身不是业务备份；密钥经既有受控渠道恢复，不装进源码包。

本机数据快照新导出使用 `mplus-cloud-data-snapshot/v2`，逐文件保留 SHA、大小、权限和纳秒级修改时间；素材包来源校验依赖该时间，不能仅凭 ZIP 内容相同判定包可用。当前恢复器兼容读取 v1：对明确声明的素材包及快照内已发布包，仅当冻结来源的大小、完整 SHA 和原修改时间一致且文件在快照内时，补回来源修改时间；不改包内容或放宽来源检查。依据缺失、跨目录或同一来源时间冲突时不猜测，结果中的 `legacy_package_metadata.warnings` 保留待核对项，不能据此声称完整恢复。v2 必须使用支持 v2 的恢复器。恢复到新目录后，经 `LocalPackageRegistry.register` 重新登记同内容包的本机路径并核对原别名保持；运行配置按新根目录重建，Provider 密钥仍走独立受控渠道。

隔离恢复演练需记录故障确认时间、最后可恢复的业务提交时间、恢复开始与完成时间，并逐项核对数据库引用、文件 SHA、旧版本、图片来源、交付文件和未决任务。恢复后先只读核验关键对象；写验证限隔离副本，未知 Provider 请求按原回执对账，不自动重新发起付费请求。测得的丢失窗口必须≤15 分钟，故障确认至服务恢复必须≤2 小时；超标或缺少计时/引用证据时保持未验收。

本次仅校准配置和演练要求，没有执行生产恢复、云策略变更、密钥读取或备份删除。真实隔离演练待环境、可恢复集合及相应授权齐备后执行，不能以配置文件存在宣称已达到恢复指标。
