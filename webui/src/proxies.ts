import type { ProxyInfo } from "./types";

export function proxyLabel(node: ProxyInfo): string {
  if (node.status === "unbound") return "未绑定";
  if (node.status === "missing") return `节点缺失：${node.id}`;
  const host = node.host?.includes(":") && !node.host.startsWith("[")
    ? `[${node.host}]` : node.host;
  return node.name || (host ? `${node.scheme}://${host}:${node.port}` : node.id || "未绑定");
}

export function proxyStatus(node: ProxyInfo): { label: string; tone: string } {
  if (node.status === "untested") return { label: "未测试", tone: "" };
  if (node.status === "unbound") return { label: "未绑定", tone: "danger" };
  if (node.status === "missing") return { label: "节点缺失", tone: "danger" };
  if (node.status === "invalid") return { label: "配置无效", tone: "danger" };
  if (node.status === "error" || !node.active || node.failure_count || node.last_error) {
    return { label: "不可用", tone: "danger" };
  }
  return { label: "可用", tone: "ok" };
}
