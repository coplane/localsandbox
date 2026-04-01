/**
 * Wire protocol types for localsandbox.
 *
 * SDK ↔ Server: request/response envelopes (this file, "Server*" types)
 * Server ↔ Runner: execution envelopes (this file, "Runner*" / envelope types)
 */

// ============================================================================
// SDK ↔ Server protocol
// ============================================================================

interface BaseRequest {
  id: string;
}

export interface BashRequest extends BaseRequest {
  type: "bash";
  command: string;
  cwd: string;
  limits?: { maxLoopIterations?: number; maxCommandCount?: number };
}

export interface SeedRequest extends BaseRequest {
  type: "seed";
  files: Record<string, string | { base64: string }>;
}

export interface ReadFileRequest extends BaseRequest {
  type: "read_file";
  path: string;
  binary?: boolean;
}

export interface WriteFileRequest extends BaseRequest {
  type: "write_file";
  path: string;
  content: string;
  binary?: boolean;
}

export interface ListFilesRequest extends BaseRequest {
  type: "list_files";
  path: string;
}

export interface ExistsRequest extends BaseRequest {
  type: "exists";
  path: string;
}

export interface DeleteFileRequest extends BaseRequest {
  type: "delete_file";
  path: string;
}

export interface KVGetRequest extends BaseRequest {
  type: "kv_get";
  key: string;
}

export interface KVSetRequest extends BaseRequest {
  type: "kv_set";
  key: string;
  value: string;
}

export interface KVDeleteRequest extends BaseRequest {
  type: "kv_delete";
  key: string;
}

export interface KVKeysRequest extends BaseRequest {
  type: "kv_keys";
  prefix: string;
}

export interface CheckpointRequest extends BaseRequest {
  type: "checkpoint";
}

export interface HistoryRequest extends BaseRequest {
  type: "history";
  limit: number;
}

export interface ExecutePythonRequest extends BaseRequest {
  type: "execute_python";
  code: string;
  cwd: string;
  preload_packages?: string[];
  tools?: Array<{ name: string; [key: string]: unknown }>;
}

export interface ShutdownRequest extends BaseRequest {
  type: "shutdown";
}

export type ServerRequest =
  | BashRequest
  | SeedRequest
  | ReadFileRequest
  | WriteFileRequest
  | ListFilesRequest
  | ExistsRequest
  | DeleteFileRequest
  | KVGetRequest
  | KVSetRequest
  | KVDeleteRequest
  | KVKeysRequest
  | CheckpointRequest
  | HistoryRequest
  | ExecutePythonRequest
  | ShutdownRequest;

// ============================================================================
// Server ↔ Python Runner protocol (unchanged)
// ============================================================================

export interface RunnerStartEnvelope {
  type: "start";
  fs_root: string;
  code: string;
  cwd: string;
  preload_packages?: string[];
  tools?: Array<{ name: string }>;
}

export interface ExecuteEnvelope {
  type: "execute";
  code: string;
  cwd: string;
  preload_packages?: string[];
}

export interface ToolCallEnvelope {
  type: "tool_call";
  id: string;
  name: string;
  payload: unknown;
}

export interface ToolResultEnvelope {
  type: "tool_result";
  id: string;
  payload: unknown;
}

export interface ToolErrorEnvelope {
  type: "tool_error";
  id: string;
  error_type: string;
  message: string;
}

export interface CompleteEnvelope {
  type: "complete";
  stdout: string;
  stderr: string;
  exit_code: number;
  error?: string;
}

export interface FatalErrorEnvelope {
  type: "fatal_error";
  message: string;
}
