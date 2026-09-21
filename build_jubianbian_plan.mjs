import fs from "node:fs/promises";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const outputDir = "outputs/jubianbian-system-plan";
await fs.mkdir(outputDir, { recursive: true });

const workbook = Workbook.create();
const main = workbook.worksheets.add("开发情况与规则");
const cost = workbook.worksheets.add("成本与部署");

const colors = {
  navy: "#1F4E78",
  blue: "#D9EAF7",
  lightBlue: "#EEF5FB",
  red: "#FCE4D6",
  redText: "#9C0006",
  yellow: "#FFF2CC",
  yellowText: "#7F6000",
  green: "#E2F0D9",
  greenText: "#375623",
  gray: "#F2F2F2",
  border: "#D9E2F3",
  white: "#FFFFFF",
  text: "#1F2937",
};

const font = { name: "Aptos", size: 10, color: colors.text };

function setBase(sheet, range) {
  sheet.getRange(range).format.font = font;
  sheet.getRange(range).format.verticalAlignment = "center";
  sheet.getRange(range).format.wrapText = true;
}

function styleHeader(sheet, range, fill = colors.navy) {
  const r = sheet.getRange(range);
  r.format = {
    fill,
    font: { name: "Aptos", size: 10, bold: true, color: colors.white },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
    borders: { preset: "all", style: "thin", color: colors.white },
  };
}

function styleSection(sheet, range, fill = colors.blue) {
  const r = sheet.getRange(range);
  r.format = {
    fill,
    font: { name: "Aptos", size: 10, bold: true, color: colors.navy },
    verticalAlignment: "center",
    wrapText: true,
    borders: { preset: "outside", style: "thin", color: colors.border },
  };
}

const rows = [
  ["现状判断", "当前开发阶段", "已完成上传视频→后台处理→结构化剧本→查看结果→Markdown/TXT下载的单机MVP闭环。", "可以本地运行和演示；还没有多用户、真实额度、管理员后台。", "P0-高", "否", 0, 0, 0, 0, 0, 0, "现状总结，不单独计价"],

  ["账户与权限", "邮箱注册", "邮箱注册，不做邮箱验证；注册时赠送额度。", "赠送额度数量待确认；短信注册首期不做。", "P0-高", "赠送额度需确认", 800, 1500, 6, 12, 0.75, 1.5, "邮箱比短信便宜，首期无需短信成本"],
  ["账户与权限", "密码规则", "只允许字母和数字，8位。", "按当前产品规则实现。", "P0-高", "否", 200, 400, 2, 4, 0.25, 0.5, "建议由后端和前端同时校验"],
  ["账户与权限", "邮箱登录", "邮箱+密码登录，登录失败给出明确提示。", "邮箱不存在、密码错误、账号禁用等提示。", "P0-高", "否", 500, 800, 4, 8, 0.5, 1, ""],
  ["账户与权限", "多设备和记住登录", "允许多设备同时登录；浏览器记住登录状态。", "按你的要求不主动过期；建议预留退出所有设备接口。", "P0-高", "退出所有设备是否首期做", 700, 1200, 6, 12, 0.75, 1.5, "持久登录Token/Session"],
  ["账户与权限", "退出登录和失败提示", "退出当前设备登录；所有登录错误有用户可理解的提示。", "首期必须有。", "P0-高", "否", 300, 500, 2, 4, 0.25, 0.5, "可与登录功能合并开发"],
  ["账户与权限", "找回密码", "后续通过邮箱重置密码。", "与合伙人确认后再开发；暂不做短信找回。", "P1-沟通", "是", 400, 800, 4, 8, 0.5, 1, "邮件成本通常低于短信"],
  ["账户与权限", "角色", "只设置普通用户、管理员两个角色。", "首期不增加运营、财务等细分角色。", "P0-高", "否", 400, 700, 3, 6, 0.375, 0.75, ""],
  ["账户与权限", "权限控制", "普通用户只能看自己的任务和额度；管理员可以进入后台。", "服务端接口必须校验权限，不能只在前端隐藏。", "P0-高", "否", 700, 1200, 8, 16, 1, 2, "包含任务数据隔离"],

  ["用户控制台", "控制台首页", "显示当前余额、总用量、任务总数、处理中任务、最近任务。", "用户登录后看到控制台，不直接显示内部模型和服务商。", "P0-高", "否", 700, 1200, 8, 16, 1, 2, ""],
  ["用户控制台", "任务中心", "任务列表、搜索、状态筛选、分页、排序、删除、重试。", "任务仅属于当前用户。", "P0-高", "否", 800, 1500, 12, 24, 1.5, 3, "现有任务列表可复用一部分"],
  ["用户控制台", "任务处理状态", "显示上传、排队、提取台词、分析画面、整理剧本、完成、失败。", "只展示用户需要的信息，不展示模型和服务商。", "P0-高", "否", 600, 1000, 8, 16, 1, 2, ""],
  ["用户控制台", "任务处理基础日志", "展示任务处理阶段、完成时间、失败原因、重试入口。", "用户操作日志暂时不做。", "P0-高", "否", 500, 900, 6, 12, 0.75, 1.5, ""],
  ["用户控制台", "个人资料", "显示注册时间；支持修改密码。", "不做头像、昵称、手机号等资料项。", "P0-高", "否", 400, 700, 4, 8, 0.5, 1, ""],
  ["用户控制台", "使用量", "显示当前余额和历史总用量。", "不做复杂图表；后期可扩展按日/月统计。", "P0-高", "否", 300, 600, 3, 6, 0.375, 0.75, ""],
  ["用户控制台", "用户操作日志", "登录、下载、删除等用户行为日志。", "首期明确不做。", "P2-后置", "否", 500, 1000, 8, 16, 1, 2, "后期需要时再开启"],

  ["额度与账务", "额度账户", "每个用户独立拥有可用余额。", "注册赠送、充值、扣费、退款都进入账户。", "P0-高", "否", 500, 900, 6, 12, 0.75, 1.5, "不能把余额写死在接口返回值里"],
  ["额度与账务", "任务扣费和退款", "创建任务前检查余额；任务创建时预扣；成功扣费；失败退款。", "按视频时长向上取整的规则仍需确认。", "P0-高", "计费规则需确认", 1200, 2000, 16, 32, 2, 4, "必须在服务端完成事务处理"],
  ["额度与账务", "额度流水", "记录充值、注册赠送、任务扣费、失败退款、管理员修正等。", "流水不可直接覆盖，支持追溯。", "P0-高", "流水类型需确认", 700, 1200, 8, 16, 1, 2, ""],
  ["额度与账务", "管理员手动充值", "管理员选择用户、输入额度、填写备注并确认充值。", "首期先做人工充值，给在线支付预留接口。", "P0-高", "否", 700, 1200, 8, 16, 1, 2, ""],
  ["额度与账务", "在线支付充值", "微信、支付宝等在线充值。", "第二阶段再做；首期不接支付。", "P1-沟通", "是", 2000, 5000, 24, 48, 3, 6, "需支付商户、回调、对账和退款"],
  ["额度与账务", "套餐系统", "体验版、基础版、专业版等套餐。", "套餐规则和价格与合伙人确认后再做。", "P1-沟通", "是", 1000, 2500, 16, 32, 2, 4, "数据库首期预留package_id"],

  ["上传与任务队列", "批量上传", "每次最多上传10个视频。", "保留当前MP4、500MB、6分钟限制，是否调整需确认。", "P0-高", "文件限制需确认", 800, 1500, 12, 24, 1.5, 3, ""],
  ["上传与任务队列", "批量任务创建", "一次上传多个文件并分别生成任务。", "某个文件失败不影响其他文件。", "P0-高", "否", 500, 1000, 6, 12, 0.75, 1.5, ""],
  ["上传与任务队列", "持久化任务队列", "任务进入队列，由Worker处理；服务重启后任务不丢失。", "首期不建议继续只使用FastAPI进程内后台任务。", "P0-高", "否", 800, 1500, 16, 32, 2, 4, "Redis+Worker或同等方案"],
  ["上传与任务队列", "并发控制", "支持约50人在线；AI识别同时处理3～5个，其余排队。", "50人在线不等于50个任务同时调用AI。", "P0-高", "并发数需确认", 600, 1200, 8, 16, 1, 2, "取决于API配额和预算"],
  ["上传与任务队列", "自动重试", "网络失败、服务商失败时自动重试；超过次数转失败。", "重试次数需确认。", "P0-高", "重试次数需确认", 500, 900, 6, 12, 0.75, 1.5, "避免重复扣费"],
  ["上传与任务队列", "取消任务", "用户或管理员取消排队任务。", "首期可后置。", "P2-后置", "是", 500, 900, 8, 16, 1, 2, "需要定义取消后的退款规则"],

  ["任务结果", "基础任务详情", "显示视频信息、处理状态、剧本结果和下载入口。", "交互细节与合伙人沟通；首期保留现有结果页。", "P1-沟通", "是", 800, 1500, 12, 24, 1.5, 3, ""],
  ["任务结果", "视频与剧本联动", "点击剧本内容跳转到视频对应时间。", "与合伙人确认是否需要。", "P1-沟通", "是", 1000, 2000, 16, 32, 2, 4, "需要时间轴数据"],
  ["任务结果", "剧本在线编辑", "修改场景、人物、对白、动作和情绪。", "与合伙人确认交互和字段后再做。", "P1-沟通", "是", 1500, 3000, 24, 48, 3, 6, ""],
  ["任务结果", "版本管理", "保留原始识别版和用户修改版，可回滚。", "后期开发，数据库预留script_versions。", "P2-后置", "是", 1000, 2500, 16, 40, 2, 5, ""],
  ["任务结果", "Word/PDF导出", "在现有Markdown/TXT基础上增加Word或PDF。", "是否首期支持Word与合伙人沟通。", "P1-沟通", "是", 800, 1500, 8, 16, 1, 2, "当前Word按钮仍是占位"],

  ["管理员后台", "后台首页", "用户数、任务数、成功数、失败数、额度消耗、队列长度。", "统计口径需确认。", "P0-高", "统计口径需确认", 800, 1500, 12, 24, 1.5, 3, ""],
  ["管理员后台", "用户管理", "搜索用户、查看详情、启用/禁用、查看余额和总用量。", "普通管理员和超级管理员不再细分。", "P0-高", "否", 1000, 1800, 16, 32, 2, 4, ""],
  ["管理员后台", "后台充值和额度修正", "给用户充值、扣减额度、填写原因、自动生成流水。", "是否允许管理员扣减额度需确认。", "P0-高", "扣减规则需确认", 800, 1400, 12, 24, 1.5, 3, ""],
  ["管理员后台", "任务管理", "查看全平台任务、按用户筛选、查看失败原因、重试、删除。", "删除规则需确认。", "P0-高", "删除规则需确认", 800, 1400, 12, 24, 1.5, 3, ""],
  ["管理员后台", "管理员操作审计", "记录充值、扣减、禁用用户、删除任务、修改配置。", "用户操作日志不做，但管理员敏感操作建议必须记录。", "P0-高", "否", 500, 900, 6, 12, 0.75, 1.5, ""],
  ["管理员后台", "系统配置", "上传限制、额度规则、并发数、任务保留时间等后台配置。", "首期可以少量配置，详细规则与合伙人确认。", "P1-沟通", "是", 800, 1500, 12, 24, 1.5, 3, ""],
  ["管理员后台", "API成本统计", "管理员查看视频分钟数、API调用量和估算成本。", "用户端不显示模型和服务商；后台可后置。", "P2-后置", "是", 1000, 2000, 16, 32, 2, 4, ""],

  ["数据库", "数据库重构", "新增users、sessions、roles、tasks.user_id、credit_accounts、credit_ledger、task_events、admin_audit_logs等。", "首期可由SQLite迁移到PostgreSQL；项目长期建议PostgreSQL。", "P0-高", "数据库方案需确认", 1000, 1800, 16, 32, 2, 4, ""],
  ["数据库", "项目/剧集预留", "预留projects、episodes，暂不做完整多集项目。", "是否做多集项目与合伙人确认。", "P2-后置", "是", 400, 800, 6, 12, 0.75, 1.5, ""],

  ["安全基础", "必须安全项", "密码哈希、HTTPS、服务端权限、任务归属、下载签名、服务端额度校验、上传格式/大小/时长校验、API Key保护、备份。", "这些建议列为首期必须项。", "P0-高", "安全范围需确认", 800, 1500, 12, 24, 1.5, 3, ""],
  ["安全基础", "可后置安全项", "邮箱验证、短信验证码、二次验证、高级风控、专业WAF、多地域容灾。", "首期可以不做，按用户量和风险再增加。", "P2-后置", "是", 0, 0, 0, 0, 0, 0, "不计入首期报价"],
  ["部署运维", "首期部署", "Nginx+HTTPS+FastAPI+Worker+PostgreSQL/Redis同机部署。", "适合低成本内测和早期运营。", "P0-高", "否", 800, 1500, 12, 24, 1.5, 3, "正式上线需要备份和监控"],
  ["部署运维", "对象存储直传", "浏览器直传OSS/COS，后台保存文件Key，使用临时签名下载。", "推荐视频文件不长期放在应用服务器。", "P0-高", "存储保留期限需确认", 800, 1500, 12, 24, 1.5, 3, "支持50人在线时更稳妥"],
  ["部署运维", "备份与监控", "数据库备份、服务器快照、基础健康检查和错误告警。", "高级监控可以后置。", "P0-高", "备份保留周期需确认", 500, 1000, 8, 16, 1, 2, ""],
];

// Main sheet layout
main.showGridLines = false;
main.tabColor = colors.navy;
main.mergeCells("A1:M1");
main.getRange("A1").values = [["剧编编软件系统开发情况和产品规则表"]];
main.getRange("A1:M1").format = {
  font: { name: "Aptos", size: 16, bold: true, color: colors.navy },
  horizontalAlignment: "left",
  verticalAlignment: "center",
};
main.getRange("A2:M2").merge();
main.getRange("A2").values = [["说明：开发价格按现有代码可复用、功能难易程度和交付风险综合评估；周期按1人连续开发估算，1工作日按8小时计算。6000元适合首期内测版，不代表全量正式版成本。"]];
main.getRange("A2:M2").format = { font: { name: "Aptos", size: 10, italic: true, color: "#595959" }, wrapText: true };

main.getRange("A3:F3").values = [["颜色示例", "P0-高优先级", "P1-需沟通", "P2-可后置", "已明确不做", "备注"]];
main.getRange("A3").format = { font: { name: "Aptos", size: 10, bold: true, color: colors.navy } };
main.getRange("B3").format = { fill: colors.red, font: { name: "Aptos", size: 10, bold: true, color: colors.redText }, horizontalAlignment: "center" };
main.getRange("C3").format = { fill: colors.yellow, font: { name: "Aptos", size: 10, bold: true, color: colors.yellowText }, horizontalAlignment: "center" };
main.getRange("D3").format = { fill: colors.green, font: { name: "Aptos", size: 10, bold: true, color: colors.greenText }, horizontalAlignment: "center" };
main.getRange("E3").format = { fill: colors.gray, font: { name: "Aptos", size: 10, bold: true, color: "#595959" }, horizontalAlignment: "center" };
main.getRange("F3").values = [["颜色只用于快速识别优先级，不代表完成状态"]];
main.getRange("F3").format = { font: { name: "Aptos", size: 9, color: "#595959" }, wrapText: true };

main.getRange("A4:D6").values = [
  ["开发报价概览", "价格下限（元）", "价格上限（元）", "周期说明"],
  ["全部P0高优先级", null, null, "单人累计开发周期，实际可并行压缩"],
  ["6000元首期内测版", 6000, 6000, "建议控制在10～15个工作日，暂缓沟通项和高级功能"],
];
styleHeader(main, "A4:D4");
main.getRange("B5").formulas = [["=SUMIF($E$10:$E$100,\"P0-高\",$G$10:$G$100)"]];
main.getRange("C5").formulas = [["=SUMIF($E$10:$E$100,\"P0-高\",$H$10:$H$100)"]];
main.getRange("B4:C6").format.numberFormat = "#,##0";
main.getRange("A5:D6").format = { fill: colors.lightBlue, borders: { preset: "all", style: "thin", color: colors.border }, wrapText: true };
main.getRange("A7:D7").values = [["P0单人累计周期（小时）", null, null, "按单人连续开发估算；并行开发可压缩日历周期"]];
main.getRange("B7").formulas = [["=SUMIF($E$10:$E$100,\"P0-高\",$I$10:$I$100)"]];
main.getRange("C7").formulas = [["=SUMIF($E$10:$E$100,\"P0-高\",$J$10:$J$100)"]];
main.getRange("A7:D7").format = { fill: colors.lightBlue, borders: { preset: "all", style: "thin", color: colors.border }, wrapText: true };
main.getRange("B7:C7").format.numberFormat = "#,##0";

const headerRow = 9;
main.getRange(`A${headerRow}:M${headerRow}`).values = [[
  "模块", "功能/事项", "产品规则或需求", "首期口径/当前状态", "优先级", "合伙人确认", "价格下限（元）", "价格上限（元）", "小时下限", "小时上限", "工作日下限", "工作日上限", "备注",
]];
styleHeader(main, `A${headerRow}:M${headerRow}`);

const startRow = 10;
const endRow = startRow + rows.length - 1;
main.getRange(`A${startRow}:M${endRow}`).values = rows;
setBase(main, `A${startRow}:M${endRow}`);
main.getRange(`A${startRow}:M${endRow}`).format.borders = { insideHorizontal: { style: "thin", color: "#E7E6E6" } };
main.getRange(`G${startRow}:H${endRow}`).format.numberFormat = "#,##0";
main.getRange(`I${startRow}:L${endRow}`).format.numberFormat = "0.00";
main.getRange(`G${startRow}:L${endRow}`).format.horizontalAlignment = "right";

for (let i = 0; i < rows.length; i += 1) {
  const rowNumber = startRow + i;
  const priority = rows[i][4];
  const priorityCell = main.getRange(`E${rowNumber}`);
  const confirmCell = main.getRange(`F${rowNumber}`);
  priorityCell.format.font = { name: "Aptos", size: 10, bold: true, color: priority === "P0-高" ? colors.redText : priority === "P1-沟通" ? colors.yellowText : colors.greenText };
  priorityCell.format.horizontalAlignment = "center";
  confirmCell.format.horizontalAlignment = "center";
  if (priority === "P0-高") priorityCell.format.fill = colors.red;
  if (priority === "P1-沟通") { priorityCell.format.fill = colors.yellow; confirmCell.format.fill = colors.yellow; }
  if (priority === "P2-后置") priorityCell.format.fill = colors.green;
  if (rows[i][5] !== "否") confirmCell.format.fill = colors.yellow;
  if (rows[i][0] !== (i > 0 ? rows[i - 1][0] : "")) {
    main.getRange(`A${rowNumber}:M${rowNumber}`).format.borders = { top: { style: "medium", color: colors.navy }, insideHorizontal: { style: "thin", color: "#E7E6E6" } };
  }
}

// Confirmation section at bottom
const confirmHeaderRow = endRow + 3;
main.mergeCells(`A${confirmHeaderRow}:M${confirmHeaderRow}`);
main.getRange(`A${confirmHeaderRow}`).values = [["合伙人确认事项（放在表格最下面）"]];
styleSection(main, `A${confirmHeaderRow}:L${confirmHeaderRow}`, colors.yellow);
const confirmTableHeader = confirmHeaderRow + 1;
main.getRange(`A${confirmTableHeader}:F${confirmTableHeader}`).values = [["编号", "确认事项", "当前建议/默认方案", "影响模块", "优先级", "确认状态"]];
styleHeader(main, `A${confirmTableHeader}:F${confirmTableHeader}`, "#806000");
const confirmations = [
  [1, "注册赠送多少额度", "首期注册赠送额度，具体数值待定", "注册、额度", "P1-沟通", "待确认"],
  [2, "计费规则", "按视频实际时长向上取整，失败退款", "额度、任务队列", "P1-沟通", "待确认"],
  [3, "找回密码", "优先采用邮箱重置，短信暂不做", "账户与权限", "P1-沟通", "待确认"],
  [4, "原视频保留多久", "建议处理完成后保留7～30天，过期自动清理", "对象存储、隐私", "P1-沟通", "待确认"],
  [5, "是否永久保存视频和剧本", "建议剧本长期保存，原视频按期限清理", "存储成本", "P1-沟通", "待确认"],
  [6, "Word/PDF是否首期做", "当前已有Markdown/TXT，Word/PDF可后置", "导出", "P1-沟通", "待确认"],
  [7, "任务详情交互", "是否需要视频播放器、时间轴、剧本联动", "任务详情", "P1-沟通", "待确认"],
  [8, "剧本在线编辑", "是否首期允许修改识别结果", "剧本结果", "P1-沟通", "待确认"],
  [9, "套餐和价格", "首期手动充值，套餐第二阶段再做", "额度、支付", "P1-沟通", "待确认"],
  [10, "在线支付", "首期管理员手动充值，后期接微信/支付宝", "充值", "P1-沟通", "待确认"],
  [11, "AI并发处理数", "建议同时处理3～5个，其余排队", "任务队列、API成本", "P1-沟通", "待确认"],
  [12, "管理员扣减额度规则", "允许人工修正，但必须填写原因并记录审计", "管理员后台", "P1-沟通", "待确认"],
  [13, "管理员删除任务规则", "明确什么情况下允许后台删除任务和视频", "管理员后台、存储", "P1-沟通", "待确认"],
  [14, "数据库部署方案", "首期PostgreSQL与应用同机，后期再拆分", "数据库、运维", "P1-沟通", "待确认"],
  [15, "安全范围", "首期做必要安全项，高级风控、短信、二次验证后置", "安全、登录", "P1-沟通", "待确认"],
  [16, "50人在线定义", "在线人数与同时上传/同时识别人数分别设定", "服务器、队列", "P1-沟通", "待确认"],
];
const confirmStart = confirmTableHeader + 1;
main.getRange(`A${confirmStart}:F${confirmStart + confirmations.length - 1}`).values = confirmations;
setBase(main, `A${confirmStart}:F${confirmStart + confirmations.length - 1}`);
main.getRange(`A${confirmStart}:F${confirmStart + confirmations.length - 1}`).format.borders = { insideHorizontal: { style: "thin", color: "#E7E6E6" } };
main.getRange(`A${confirmStart}:F${confirmStart + confirmations.length - 1}`).format.fill = colors.yellow;
main.getRange(`A${confirmStart}:A${confirmStart + confirmations.length - 1}`).format.horizontalAlignment = "center";
main.getRange(`E${confirmStart}:F${confirmStart + confirmations.length - 1}`).format.horizontalAlignment = "center";

// Main sheet sizing
const widths = { A: 16, B: 22, C: 42, D: 38, E: 12, F: 18, G: 13, H: 13, I: 10, J: 10, K: 11, L: 11, M: 32 };
for (const [col, width] of Object.entries(widths)) main.getRange(`${col}1:${col}${confirmStart + confirmations.length}`).format.columnWidth = width;
main.getRange("A1:M1").format.rowHeight = 28;
main.getRange("A2:M2").format.rowHeight = 34;
main.getRange(`A${headerRow}:M${headerRow}`).format.rowHeight = 34;
main.getRange(`A${startRow}:M${endRow}`).format.rowHeight = 42;
main.getRange(`A${confirmStart}:F${confirmStart + confirmations.length - 1}`).format.rowHeight = 34;
main.freezePanes.freezeRows(headerRow);

// Cost and deployment sheet
cost.showGridLines = false;
cost.tabColor = "#70AD47";
cost.mergeCells("A1:H1");
cost.getRange("A1").values = [["成本与部署估算"]];
cost.getRange("A1:H1").format = { font: { name: "Aptos", size: 16, bold: true, color: colors.navy }, horizontalAlignment: "left", verticalAlignment: "center" };
cost.mergeCells("A2:H2");
cost.getRange("A2").values = [["以下为首期推荐的低成本方案；固定运营成本不含AI API调用费，API费用按实际视频分钟数和服务商价格结算。"]];
cost.getRange("A2:H2").format = { font: { name: "Aptos", size: 10, italic: true, color: "#595959" }, wrapText: true };

cost.getRange("A4:D7").values = [
  ["成本概览", "下限", "上限", "说明"],
  ["固定月成本（不含API）", null, null, "服务器、对象存储、备份、邮件和基础监控"],
  ["固定年成本（域名/证书等）", 50, 150, "域名；HTTPS证书可使用Let's Encrypt免费证书"],
  ["AI API成本", null, null, "变量成本，按视频处理分钟数和具体模型价格计算"],
];
styleHeader(cost, "A4:D4");
cost.getRange("B5").formulas = [["=SUMIF($F$10:$F$25,\"月\",$D$10:$D$25)"]];
cost.getRange("C5").formulas = [["=SUMIF($F$10:$F$25,\"月\",$E$10:$E$25)"]];
cost.getRange("B7:C7").values = [["按量", "按量"]];
cost.getRange("A5:D7").format = { fill: colors.lightBlue, borders: { preset: "all", style: "thin", color: colors.border }, wrapText: true };
cost.getRange("B5:C6").format.numberFormat = "#,##0";

cost.getRange("A9:H9").values = [["类别", "项目", "推荐配置/规则", "月成本下限（元）", "月成本上限（元）", "计费周期", "是否首期必需", "备注"]];
styleHeader(cost, "A9:H9");
const costRows = [
  ["服务器", "应用/API/Worker服务器", "4核8GB，10～20Mbps，100GB系统盘；首期Docker同机部署", 200, 500, "月", "是", "活动价估算；正常续费可能为¥400～800/月"],
  ["数据库", "PostgreSQL", "首期与应用同机，后期可迁移到独立托管数据库", 0, 0, "月", "是", "首期增量成本约为0，代价是单机故障风险"],
  ["任务队列", "Redis", "首期与应用同机，后期可拆到独立实例", 0, 0, "月", "是", "首期增量成本约为0"],
  ["对象存储", "OSS/COS/S3兼容存储", "500GB～1TB；浏览器直传，处理完成后按保留策略清理", 50, 200, "月", "是", "下载流量另计；原视频不建议永久保留"],
  ["备份", "快照和数据库备份", "每日或每周备份，保留2～4周", 30, 150, "月", "是", "按云厂商快照和备份空间估算"],
  ["域名", "正式域名", ".com或.cn", 0, 0, "年", "是", "年成本约¥50～150，已在概览中单列"],
  ["HTTPS", "TLS证书", "Let's Encrypt或云厂商免费证书", 0, 0, "年", "是", "不建议首期购买高价商业证书"],
  ["邮件", "注册/找回密码邮件", "首期注册不发验证邮件；找回密码后使用事务邮件", 0, 100, "月", "否", "邮件通常比短信便宜"],
  ["监控", "基础监控和错误告警", "健康检查、错误日志和基础告警", 0, 100, "月", "建议", "可先使用自建监控和免费额度"],
  ["支付", "在线支付", "首期管理员手动充值，暂不接微信/支付宝", 0, 0, "月", "否", "后续会增加支付手续费和对账开发成本"],
  ["短信", "短信验证码", "首期不做注册和登录短信", 0, 0, "月", "否", "短信需要模板、签名和按条计费"],
  ["API示例", "1000分钟×¥0.3/分钟", "仅为预算敏感性示例，不代表方舟实际报价", 300, 300, "按量", "否", "1000分钟/月时约¥300"],
  ["API示例", "1000分钟×¥0.5/分钟", "仅为预算敏感性示例，不代表方舟实际报价", 500, 500, "按量", "否", "1000分钟/月时约¥500"],
  ["API示例", "1000分钟×¥1/分钟", "仅为预算敏感性示例，不代表方舟实际报价", 1000, 1000, "按量", "否", "1000分钟/月时约¥1,000"],
  ["API示例", "5000分钟×¥1/分钟", "仅为预算敏感性示例，不代表方舟实际报价", 5000, 5000, "按量", "否", "5000分钟/月时约¥5,000"],
];
const costStart = 10;
cost.getRange(`A${costStart}:H${costStart + costRows.length - 1}`).values = costRows;
setBase(cost, `A${costStart}:H${costStart + costRows.length - 1}`);
cost.getRange(`D${costStart}:E${costStart + costRows.length - 1}`).format.numberFormat = "#,##0";
cost.getRange(`D${costStart}:E${costStart + costRows.length - 1}`).format.horizontalAlignment = "right";
cost.getRange(`A${costStart}:H${costStart + costRows.length - 1}`).format.borders = { insideHorizontal: { style: "thin", color: "#E7E6E6" } };
for (let i = 0; i < costRows.length; i += 1) {
  const rowNumber = costStart + i;
  if (costRows[i][6] === "是") cost.getRange(`G${rowNumber}`).format.fill = colors.red;
  if (costRows[i][6] === "建议") cost.getRange(`G${rowNumber}`).format.fill = colors.yellow;
  if (costRows[i][6] === "否") cost.getRange(`G${rowNumber}`).format.fill = colors.green;
  cost.getRange(`G${rowNumber}`).format.horizontalAlignment = "center";
}

const archRow = costStart + costRows.length + 2;
cost.mergeCells(`A${archRow}:H${archRow}`);
cost.getRange(`A${archRow}`).values = [["推荐低成本架构和容量假设"]];
styleSection(cost, `A${archRow}:H${archRow}`);
cost.getRange(`A${archRow + 1}:D${archRow + 1}`).values = [["项目", "首期建议", "原因", "后续升级"]];
styleHeader(cost, `A${archRow + 1}:D${archRow + 1}`);
const archRows = [
  ["上传方式", "浏览器直传OSS/COS，不经过应用服务器", "50人同时上传顶格视频时，单台服务器不适合中转250GB级别的瞬时数据", "增加CDN、分片上传和断点续传"],
  ["在线人数", "按50人在线设计", "在线人数与同时识别数量分离", "按真实峰值扩容API和Worker"],
  ["AI并发", "3～5个任务同时识别，其余排队", "控制API成本和服务商并发配额", "增加Worker实例和并发额度"],
  ["文件保留", "原视频处理完成后保留7～30天，剧本长期保存", "降低对象存储成本", "按套餐设计不同保留期限"],
  ["固定月成本", "约¥280～1,050/月，不含API", "单服务器+对象存储+备份+基础邮件/监控", "拆分数据库、Redis、Worker后约¥800～1,500/月"],
];
cost.getRange(`A${archRow + 2}:D${archRow + 1 + archRows.length}`).values = archRows;
setBase(cost, `A${archRow + 2}:D${archRow + 1 + archRows.length}`);
cost.getRange(`A${archRow + 2}:D${archRow + 1 + archRows.length}`).format.borders = { insideHorizontal: { style: "thin", color: "#E7E6E6" } };

const costWidths = { A: 14, B: 24, C: 42, D: 16, E: 16, F: 12, G: 14, H: 40 };
for (const [col, width] of Object.entries(costWidths)) cost.getRange(`${col}1:${col}${archRow + 10}`).format.columnWidth = width;
cost.getRange("A1:H1").format.rowHeight = 28;
cost.getRange("A2:H2").format.rowHeight = 32;
cost.getRange("A9:H9").format.rowHeight = 34;
cost.getRange(`A${costStart}:H${costStart + costRows.length - 1}`).format.rowHeight = 38;
cost.freezePanes.freezeRows(9);

workbook.recalculate();

const inspectMain = await workbook.inspect({
  kind: "table",
  sheetId: "开发情况与规则",
  range: `A1:M${confirmStart + confirmations.length - 1}`,
  include: "values,formulas",
  tableMaxRows: 12,
  tableMaxCols: 12,
});
console.log(inspectMain.ndjson);
const inspectCost = await workbook.inspect({
  kind: "table",
  sheetId: "成本与部署",
  range: `A1:H${archRow + 1 + archRows.length}`,
  include: "values,formulas",
  tableMaxRows: 12,
  tableMaxCols: 8,
});
console.log(inspectCost.ndjson);

const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!",
  options: { useRegex: true, maxResults: 100 },
  summary: "final formula error scan",
});
console.log(errors.ndjson);

const previewMain = await workbook.render({ sheetName: "开发情况与规则", range: `A1:M${confirmStart + 12}`, scale: 1, format: "png" });
await fs.writeFile(`${outputDir}/开发情况与规则预览.png`, new Uint8Array(await previewMain.arrayBuffer()));
const previewCost = await workbook.render({ sheetName: "成本与部署", range: `A1:H${archRow + 1 + archRows.length}`, scale: 1, format: "png" });
await fs.writeFile(`${outputDir}/成本与部署预览.png`, new Uint8Array(await previewCost.arrayBuffer()));

const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(`${outputDir}/剧编编软件系统开发情况和产品规则表.xlsx`);
console.log(`SAVED: ${outputDir}/剧编编软件系统开发情况和产品规则表.xlsx`);
