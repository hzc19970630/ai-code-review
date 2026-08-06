"use strict";

const fs = require("node:fs");

const PROTOCOL_VERSION = "syntax-analyzer/v1";
const TYPESCRIPT_VERSION = "5.9.3";

function emit(value) {
  process.stdout.write(JSON.stringify(value));
}

function sourceLine(content, lineNumber) {
  return content.split(/\r?\n/u)[lineNumber - 1] || "";
}

try {
  const request = JSON.parse(fs.readFileSync(0, "utf8"));
  if (
    request.protocol_version !== PROTOCOL_VERSION ||
    typeof request.file_path !== "string" ||
    typeof request.content !== "string" ||
    !["TS", "TSX"].includes(request.script_kind)
  ) {
    throw new Error("invalid analyzer request");
  }

  const ts = require("typescript");
  if (ts.version !== TYPESCRIPT_VERSION) {
    emit({
      protocol_version: PROTOCOL_VERSION,
      status: "failed",
      reason_code: "ANALYZER_UNAVAILABLE",
      diagnostics: [],
    });
    process.exit(0);
  }

  const scriptKind = request.script_kind === "TSX" ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const sourceFile = ts.createSourceFile(
    request.file_path,
    request.content,
    ts.ScriptTarget.Latest,
    true,
    scriptKind,
  );
  const compilerOptions = {
    allowJs: false,
    jsx: ts.JsxEmit.Preserve,
    module: ts.ModuleKind.ESNext,
    noLib: true,
    noResolve: true,
    skipLibCheck: true,
    target: ts.ScriptTarget.Latest,
  };
  const compilerHost = {
    fileExists: (fileName) => fileName === request.file_path,
    getCanonicalFileName: (fileName) => fileName,
    getCurrentDirectory: () => ".",
    getDefaultLibFileName: () => "",
    getDirectories: () => [],
    getNewLine: () => "\n",
    getSourceFile: (fileName) => fileName === request.file_path ? sourceFile : undefined,
    readFile: (fileName) => fileName === request.file_path ? request.content : undefined,
    useCaseSensitiveFileNames: () => true,
    writeFile: () => {},
  };
  const program = ts.createProgram([request.file_path], compilerOptions, compilerHost);
  const programSourceFile = program.getSourceFile(request.file_path);
  if (!programSourceFile) {
    throw new Error("source file was not added to the parser program");
  }
  const parserDiagnostics = [
    ...program.getSyntacticDiagnostics(programSourceFile),
    ...program.getSemanticDiagnostics(programSourceFile).filter(
      (diagnostic) => diagnostic.code >= 1000 && diagnostic.code < 2000,
    ),
  ];
  const uniqueDiagnostics = Array.from(
    new Map(
      parserDiagnostics.map((diagnostic) => [
        `${diagnostic.code}:${diagnostic.start}:${diagnostic.length}`,
        diagnostic,
      ]),
    ).values(),
  );
  const diagnostics = uniqueDiagnostics.map((diagnostic) => {
    const start = typeof diagnostic.start === "number" ? diagnostic.start : 0;
    const end = start + (typeof diagnostic.length === "number" ? diagnostic.length : 0);
    const startPosition = sourceFile.getLineAndCharacterOfPosition(start);
    const endPosition = sourceFile.getLineAndCharacterOfPosition(end);
    const lineStart = startPosition.line + 1;
    return {
      code: `TS${diagnostic.code}`,
      message: ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n"),
      line_start: lineStart,
      column_start: startPosition.character + 1,
      line_end: endPosition.line + 1,
      column_end: endPosition.character + 1,
      literal_evidence: sourceLine(request.content, lineStart),
    };
  });
  diagnostics.sort((left, right) =>
    left.line_start - right.line_start ||
    left.column_start - right.column_start ||
    left.code.localeCompare(right.code) ||
    left.message.localeCompare(right.message),
  );
  emit({
    protocol_version: PROTOCOL_VERSION,
    status: "ok",
    diagnostics,
  });
} catch (error) {
  emit({
    protocol_version: PROTOCOL_VERSION,
    status: "failed",
    reason_code: "ANALYZER_CRASH",
    diagnostics: [],
  });
  process.exitCode = 1;
}
