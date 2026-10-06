// ESLint flat config used by lib/cxmetrics/nodetools.py (NOT by the legacy js/analyze.sh).
// Reports, for every function-like node: name, line span and parameter count (rule cx/fn, JSON message),
// built-in cyclomatic complexity (threshold 0 reports every function) and SonarJS cognitive complexity
// (threshold 0 reports every function with a non-zero score). TypeScript is parsed by @typescript-eslint/parser.
import sonarjs from "eslint-plugin-sonarjs";
import tsParser from "@typescript-eslint/parser";

function fnName(node) {
  if (node.id && node.id.name) return node.id.name;
  const p = node.parent;
  if (!p) return "<anonymous>";
  if (p.type === "VariableDeclarator" && p.id.type === "Identifier") return p.id.name;
  if ((p.type === "MethodDefinition" || p.type === "Property" || p.type === "PropertyDefinition") && p.key) {
    return p.key.name || p.key.value || "<computed>";
  }
  if (p.type === "AssignmentExpression" && p.left.type === "MemberExpression" && p.left.property) {
    return p.left.property.name || "<computed>";
  }
  return "<anonymous>";
}

function exportNames(node) {
  const out = [];
  if (node.type === "ExportDefaultDeclaration") return ["default"];
  if (node.type !== "ExportNamedDeclaration") return out;
  const d = node.declaration;
  if (d) {
    if (d.id && d.id.name) out.push(d.id.name);
    else if (d.declarations) for (const v of d.declarations) if (v.id && v.id.name) out.push(v.id.name);
  }
  for (const sp of node.specifiers || []) out.push((sp.exported && (sp.exported.name || sp.exported.value)) || "?");
  return out;
}

const cx = {
  rules: {
    // one report per function-like node; span includes the method/property head so that the position of a
    // complexity report (which sits on the function head) always falls inside the span of its function.
    fn: {
      meta: { type: "suggestion", schema: [] },
      create(context) {
        const visit = (node) => {
          const p = node.parent;
          const methodLike = p && (p.type === "MethodDefinition" || p.type === "Property" || p.type === "PropertyDefinition") && p.value === node;
          const cls = methodLike && p.type === "MethodDefinition" && p.parent && p.parent.parent && p.parent.parent.id ? p.parent.parent.id.name : "";
          const start = methodLike ? p.loc.start : node.loc.start;
          const name = fnName(node);
          context.report({
            node,
            message: JSON.stringify({
              name, cls, params: node.params.length, sl: start.line, sc: start.column + 1,
              el: node.loc.end.line, ec: node.loc.end.column + 1, anon: name === "<anonymous>",
            }),
          });
        };
        return { FunctionDeclaration: visit, FunctionExpression: visit, ArrowFunctionExpression: visit };
      },
    },
    // names exported by the file (value and type exports), once per file
    exports: {
      meta: { type: "suggestion", schema: [] },
      create(context) {
        const names = [];
        const add = (node) => { names.push(...exportNames(node)); };
        return {
          ExportNamedDeclaration: add,
          ExportDefaultDeclaration: add,
          "Program:exit"(node) {
            if (names.length) context.report({ node, message: JSON.stringify({ exports: names }) });
          },
        };
      },
    },
  },
};

export default [
  {
    files: ["**/*.js", "**/*.jsx", "**/*.mjs", "**/*.cjs", "**/*.ts", "**/*.tsx", "**/*.mts", "**/*.cts"],
    languageOptions: {
      parser: tsParser,
      ecmaVersion: "latest",
      sourceType: "module",
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    linterOptions: { reportUnusedDisableDirectives: "off", noInlineConfig: true },
    plugins: { sonarjs, cx },
    rules: {
      "cx/fn": "warn",
      "cx/exports": "warn",
      "complexity": ["warn", { max: 0, variant: "classic" }],
      "sonarjs/cognitive-complexity": ["warn", 0],
    },
  },
];
