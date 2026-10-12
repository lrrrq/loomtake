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
