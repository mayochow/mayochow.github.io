# Mayo 自用 Sileo 软件源

在 Sileo 添加：**https://mayochow.github.io/**

## 平时如何维护

打开 `debs/` 文件夹，选择 **Add file → Upload files** 上传 DEB，提交到 `main`。
Actions 会自动读取包信息、分类、生成 APT 索引并发布，再检查公网索引和每个 DEB 的校验值。
删除某个 DEB 并提交，下一次发布就会移除它。更新插件时上传新版，旧版可保留或删除。
不要重复上传相同包名、版本和架构；替换插件内容应提升版本号。

## 自动分类

| DEB 内部 Architecture | 分类 |
| --- | --- |
| iphoneos-arm | rootful |
| iphoneos-arm64 | rootless |
| iphoneos-arm64e | roothide |

判断依据是 DEB control 的完整包架构字符串，不是文件名、手机型号或 dylib 的 CPU 切片。
分类不会转换插件，也不能证明实际设备兼容性。依赖、系统版本与运行效果仍由原包决定。
对于 `Architecture: all`，默认停止构建。人工确认适用环境后，在 `repo-config.json`
的 `all_assignments` 中用该包 SHA256 映射到适用分类列表。
未知架构、明显路径冲突、损坏包或重复版本会停止发布，保留上次成功部署。

源首页显示三个子源地址，也可只添加适用环境的子源。一般只需添加上面的主源。
`classification.json` 记录原文件、包名、版本、架构、SHA256 和分类。
原始 DEB 留在 `debs/`，发布时自动归类，不需要手动移动。

## 查看是否发布成功

打开 **Actions → Build and publish Sileo repo**，确认 build 和 deploy 都通过。
失败时打开红色任务查看原因。手动重跑可点 **Run workflow**。
部署后检查 https://mayochow.github.io/Release 和 https://mayochow.github.io/Packages 。
本源未配置 GPG 签名；公网检查不代替手机上的 Sileo 添加及安装验证。

Pages 设置使用 **GitHub Actions**；只发布工作流生成的 `_site/`。
无需 PAT、无需配置 Secrets。公开仓库和 Pages 的包可被其他人下载。
旧工具的 `overrides.json`、`sections.json` 和旧索引脚本不再参与发布。
显示名默认取自 DEB 自身，主源分类使用 rootful/rootless/roothide。

## 自己改插件的显示名称和说明

打开仓库根目录 `display-overrides.json`，点击铅笔编辑。
在插件对应的 Package 标识下面修改 `name`（名称）、`description`（说明），
点 **Commit changes**，提交到 main。自动发布成功后在 Sileo 刷新软件源。
文件已填入现有插件的原名称与原说明，可直接改这两项。

标识如 `local.albumcreationguard.roothide` 用来准确找到插件，请保留不变。
新插件可在这个 JSON 对象里增加同样格式的条目，条目之间用英文逗号分隔。
包标识可以在 `classification.json` 的 `package` 字段查看。
上传前写好规则或上传后再改都可以；同包的新版本继续沿用规则。
删除某条 name 或 description 配置项会恢复使用 DEB 自身对应文字。
说明换行使用 JSON 的换行转义，名称保持一行，标点引号使用英文格式。

这只改变源索引里展示的文字，原 DEB 的字节、Package、Version、Architecture、
依赖及校验值均不变；不会修改手机设置里的插件标题或应用图标名称。
若需要下载的 DEB 本身也带新名称，要改原包 control 中的 Name 和 Description，
重新打包后上传；同包同架构替换已有发布内容时，应递增 Version。

## 本地检查（可选）

```sh
python -m unittest discover -s tests -v
python scripts/build_repo.py --base-url https://mayochow.github.io/
python scripts/verify_repo.py --base-url https://mayochow.github.io/
```

需要 Python 3.10+，读取 zstd 包还需 zstd。输出目录已存在时拒绝覆盖。
Ubuntu Actions 使用 dpkg 和 APT 做独立验证，不安装插件、不执行安装脚本。
