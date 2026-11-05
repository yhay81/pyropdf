// Sample only the explicitly launched rendering worker's main thread.
// Build in Developer PowerShell: cl /EHsc /O2 sample_windows.cpp /link dbghelp.lib
// GetThreadContext requires suspension; always resume before symbol lookup.
// https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-getthreadcontext
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <dbghelp.h>
#include <chrono>
#include <iostream>
#include <map>
#include <string>
#include <thread>

static std::string quoted(const std::string& value) {
    std::string result = "\"";
    for (unsigned char c : value) {
        if (c == '"' || c == '\\') result += '\\';
        if (c >= 32) result += c;
    }
    return result + '"';
}

int main(int argc, char** argv) {
    if (argc != 5) return 2;
    const DWORD pid = std::stoul(argv[1]);
    const DWORD tid = std::stoul(argv[2]);
    const int seconds = std::stoi(argv[3]);
    if (seconds < 1 || seconds > 60) return 2;
    HANDLE process = OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, FALSE, pid);
    HANDLE thread = OpenThread(THREAD_GET_CONTEXT | THREAD_SUSPEND_RESUME | THREAD_QUERY_LIMITED_INFORMATION, FALSE, tid);
    if (!process || !thread) {
        std::cerr << "Open handle failed: " << GetLastError() << '\n';
        if (thread) CloseHandle(thread);
        if (process) CloseHandle(process);
        return 1;
    }
    if (GetProcessIdOfThread(thread) != pid) {
        std::cerr << "Thread does not belong to the rendering worker\n";
        CloseHandle(thread);
        CloseHandle(process);
        return 1;
    }
    SymSetOptions(SYMOPT_UNDNAME | SYMOPT_DEFERRED_LOADS | SYMOPT_FAIL_CRITICAL_ERRORS | SYMOPT_NO_PROMPTS);
    if (!SymInitialize(process, argv[4], TRUE)) {
        std::cerr << "SymInitialize failed: " << GetLastError() << '\n';
        CloseHandle(thread);
        CloseHandle(process);
        return 1;
    }
    std::cout << "ready" << std::endl;
    std::string start;
    std::getline(std::cin, start);
    std::map<DWORD64, unsigned> addresses;
    unsigned errors = 0;
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(seconds);
    while (std::chrono::steady_clock::now() < deadline) {
        CONTEXT context{};
        context.ContextFlags = CONTEXT_CONTROL;
        if (SuspendThread(thread) == DWORD(-1)) {
            ++errors;
            break;
        }
        const BOOL captured = GetThreadContext(thread, &context);
        const DWORD resumed = ResumeThread(thread);
        if (resumed == DWORD(-1)) {
            std::cerr << "ResumeThread failed: " << GetLastError() << '\n';
            SymCleanup(process);
            CloseHandle(thread);
            CloseHandle(process);
            return 1;
        }
        if (captured) ++addresses[context.Rip];
        else ++errors;
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
    SymRefreshModuleList(process);
    std::map<std::string, unsigned> symbols;
    std::map<DWORD64, unsigned> modules;
    unsigned unresolved = 0;
    unsigned missing_module = 0;
    for (const auto& entry : addresses) {
        const DWORD64 module_base = SymGetModuleBase64(process, entry.first);
        if (module_base) modules[module_base] += entry.second;
        alignas(SYMBOL_INFO) char buffer[sizeof(SYMBOL_INFO) + 4096]{};
        auto symbol = reinterpret_cast<SYMBOL_INFO*>(buffer);
        symbol->SizeOfStruct = sizeof(SYMBOL_INFO);
        symbol->MaxNameLen = 4096;
        DWORD64 displacement = 0;
        if (SymFromAddr(process, entry.first, &displacement, symbol)) {
            symbols[std::string(symbol->Name, symbol->NameLen)] += entry.second;
        } else {
            unresolved += entry.second;
            if (!module_base) missing_module += entry.second;
        }
    }
    std::cout << "{\"errors\":" << errors << ",\"unresolved\":" << unresolved
              << ",\"missing_module\":" << missing_module << ",\"symbols\":{";
    bool first = true;
    for (const auto& entry : symbols) {
        if (!first) std::cout << ',';
        first = false;
        std::cout << quoted(entry.first) << ':' << entry.second;
    }
    std::cout << "},\"modules\":[";
    first = true;
    for (const auto& entry : modules) {
        IMAGEHLP_MODULE64 info{};
        info.SizeOfStruct = sizeof(info);
        if (!SymGetModuleInfo64(process, entry.first, &info)) continue;
        if (!first) std::cout << ',';
        first = false;
        std::cout << "{\"name\":" << quoted(info.ModuleName) << ",\"samples\":" << entry.second
                  << ",\"symbol_type\":" << info.SymType
                  << ",\"pdb_unmatched\":" << (info.PdbUnmatched ? "true" : "false") << '}';
    }
    std::cout << "]}" << std::endl;
    SymCleanup(process);
    CloseHandle(thread);
    CloseHandle(process);
    return errors ? 1 : 0;
}
