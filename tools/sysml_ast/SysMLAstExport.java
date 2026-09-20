import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;

import org.eclipse.emf.ecore.EAttribute;
import org.eclipse.emf.ecore.EObject;
import org.eclipse.emf.ecore.EReference;
import org.eclipse.emf.ecore.resource.Resource;
import org.omg.sysml.interactive.SysMLInteractive;

/**
 * SysML v2 语法树导出（胶水层，2026-09-20）。
 *
 * 目的：**不要再自己写 SysML 解析器**。工程里的 `checker.jar` 就是
 * OMG SysML v2 官方参考实现（Pilot Implementation）+ Eclipse Xtext 语法，
 * 这里只做「调用它 → 把语法树搬成 JSON」，语法正确性 100% 由官方解析器负责。
 *
 * 本类不含任何 SysML 语法知识：节点类型取 `eClass().getName()`（PartUsage /
 * PartDefinition / SatisfyRequirementUsage / ConnectionUsage ...），
 * 关系取 EMF 的包含与非包含引用（引用名即语义，如 satisfiedRequirement / connectorEnd）。
 * 新增语法支持由升级 OMG 解析器获得，不需要改这里。
 *
 * 用法：java -cp "checker.jar;<outdir>" SysMLAstExport <file.sysml> [标准库目录]
 * 输出：单行 JSON {ok, roots, issues, truncated, nodes[], edges[]}
 */
public class SysMLAstExport {

    static final int MAX_NODES = 20000;

    static IdentityHashMap<EObject, String> IDS = new IdentityHashMap<>();
    static int seq = 0;
    static Resource INPUT = null;
    static boolean truncated = false;
    /** 第一遍收集到的全部元素（按包含序），供第二遍统一处理引用 —— 避免引用早于目标被访问。 */
    static List<EObject> ALL = new ArrayList<>();

    static String id(EObject o) {
        String v = IDS.get(o);
        if (v == null) {
            v = "n" + (++seq);
            IDS.put(o, v);
        }
        return v;
    }

    static String esc(String s) {
        if (s == null) return "";
        StringBuilder b = new StringBuilder(Math.max(16, s.length()));
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '"') b.append("\\\"");
            else if (c == '\\') b.append("\\\\");
            else if (c == '\n') b.append("\\n");
            else if (c == '\r') b.append("\\r");
            else if (c == '\t') b.append("\\t");
            else if (c < 0x20) b.append(String.format("\\u%04x", (int) c));
            else b.append(c);
        }
        return b.toString();
    }

    static String attr(EObject o, String name) {
        try {
            for (EAttribute a : o.eClass().getEAllAttributes()) {
                if (name.equals(a.getName())) {
                    Object v = o.eGet(a);
                    return v == null ? "" : String.valueOf(v);
                }
            }
        } catch (Throwable t) {
            // 某些派生属性会抛异常（未解析/循环），忽略即可
        }
        return "";
    }

    public static void main(String[] args) {
        try {
            if (args.length < 1) {
                System.out.println("{\"ok\":false,\"error\":\"usage: SysMLAstExport <file.sysml> [libraryDir]\"}");
                return;
            }
            String path = args[0];
            String libArg = args.length > 1 ? args[1] : "sysml.library";
            String text = Files.readString(Path.of(path), StandardCharsets.UTF_8);

            SysMLInteractive si = SysMLInteractive.getInstance();
            File lib = new File(libArg);
            if (lib.isDirectory()) {
                si.loadLibrary(lib.getAbsolutePath());
            }
            si.next(".sysml");
            si.parse(text);
            INPUT = si.getResource();

            List<Object> roots = (INPUT == null)
                    ? new ArrayList<Object>() : new ArrayList<Object>(INPUT.getContents());

            StringBuilder nodes = new StringBuilder();
            StringBuilder edges = new StringBuilder();
            for (Object o : roots) {
                if (o instanceof EObject) walk((EObject) o, null, null, nodes, edges);
            }
            // 第二遍：所有元素都已拿到 id，再统一处理非包含引用（否则会产出悬空 target）
            for (EObject o : ALL) {
                refs(o, edges);
            }

            int issues = -1;
            try {
                issues = si.validate().size();
            } catch (Throwable t) {
                issues = -1;
            }

            System.out.println("{\"ok\":true,\"roots\":" + roots.size()
                    + ",\"issues\":" + issues
                    + ",\"truncated\":" + truncated
                    + ",\"nodes\":[" + nodes + "],\"edges\":[" + edges + "]}");
        } catch (Throwable t) {
            System.out.println("{\"ok\":false,\"error\":\"" + esc(String.valueOf(t)) + "\"}");
        }
    }

    static void walk(EObject o, EObject parent, String refName, StringBuilder nodes, StringBuilder edges) {
        if (IDS.size() >= MAX_NODES) {
            truncated = true;
            return;
        }
        String myId = id(o);
        ALL.add(o);
        if (nodes.length() > 0) nodes.append(",");
        nodes.append("{\"id\":\"").append(myId)
             .append("\",\"type\":\"").append(esc(o.eClass().getName()))
             .append("\",\"name\":\"").append(esc(attr(o, "name")))
             .append("\",\"qualifiedName\":\"").append(esc(attr(o, "qualifiedName")))
             .append("\"}");

        if (parent != null && refName != null && edges.length() < 400000) {
            if (edges.length() > 0) edges.append(",");
            edges.append("{\"source\":\"").append(id(parent))
                 .append("\",\"target\":\"").append(myId)
                 .append("\",\"ref\":\"").append(esc(refName))
                 .append("\",\"kind\":\"containment\"}");
        }

        for (EObject c : new ArrayList<EObject>(o.eContents())) {
            walk(c, o, "owned", nodes, edges);
        }
    }

    /** 非包含引用 = 语义关系（satisfy / connect / dependency / 子类化 ...）。 */
    static void refs(EObject o, StringBuilder edges) {
        // resolve=false：不解析代理，避免把整个标准库拖进输出。
        for (EReference ref : o.eClass().getEAllReferences()) {
            if (ref.isContainment() || ref.isContainer()) continue;
            Object v;
            try {
                v = o.eGet(ref, false);
            } catch (Throwable t) {
                continue;
            }
            if (v == null) continue;
            if (v instanceof List) {
                for (Object t : new ArrayList<Object>((List<?>) v)) {
                    if (t instanceof EObject) addRef(edges, o, (EObject) t, ref.getName());
                }
            } else if (v instanceof EObject) {
                addRef(edges, o, (EObject) v, ref.getName());
            }
        }
    }

    static void addRef(StringBuilder edges, EObject s, EObject t, String refName) {
        if (t.eIsProxy()) return;                       // 未解析的库引用，丢弃
        if (!IDS.containsKey(t)) return;                // 目标未在第一遍收进包含树 → 不产出悬空边
        if (edges.length() > 0) edges.append(",");
        edges.append("{\"source\":\"").append(id(s))
             .append("\",\"target\":\"").append(id(t))
             .append("\",\"ref\":\"").append(esc(refName))
             .append("\",\"kind\":\"reference\"}");
    }
}
