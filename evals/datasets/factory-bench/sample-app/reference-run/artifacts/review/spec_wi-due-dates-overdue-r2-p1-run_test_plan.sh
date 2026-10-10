#!/usr/bin/env bash
# Runs every spec test_plan.commands entry verbatim in the given checkout.
# usage: run_test_plan.sh <checkout_dir> <venv_bin_dir>
cd "$1" || exit 2
export PATH="$2:$PATH"
rc_all=0
printf "=== [1] %s\n" 'python -m pytest -q'
( python -m pytest -q ) 2>&1 | tail -25; rc=${PIPESTATUS[0]}
echo "=== [1] exit $rc"; [ $rc -ne 0 ] && rc_all=1
printf "=== [2] %s\n" 'python -m pytest -q tests/test_todos.py tests/test_overdue.py'
( python -m pytest -q tests/test_todos.py tests/test_overdue.py ) 2>&1 | tail -25; rc=${PIPESTATUS[0]}
echo "=== [2] exit $rc"; [ $rc -ne 0 ] && rc_all=1
printf "=== [3] %s\n" 'test -f tests/test_overdue.py && test -f tests/e2e_overdue.py'
( test -f tests/test_overdue.py && test -f tests/e2e_overdue.py ) 2>&1 | tail -25; rc=${PIPESTATUS[0]}
echo "=== [3] exit $rc"; [ $rc -ne 0 ] && rc_all=1
printf "=== [4] %s\n" 'git diff --exit-code main -- tests/test_todos.py pyproject.toml service_manifest.json .github'
( git diff --exit-code main -- tests/test_todos.py pyproject.toml service_manifest.json .github ) 2>&1 | tail -25; rc=${PIPESTATUS[0]}
echo "=== [4] exit $rc"; [ $rc -ne 0 ] && rc_all=1
printf "=== [5] %s\n" 'test -z "$( (git diff --name-only main; git ls-files -o --exclude-standard) | grep -vxE '"'"'app/storage\.py|app/main\.py|README\.md|tests/test_overdue\.py|tests/e2e_overdue\.py'"'"' )"'
( test -z "$( (git diff --name-only main; git ls-files -o --exclude-standard) | grep -vxE 'app/storage\.py|app/main\.py|README\.md|tests/test_overdue\.py|tests/e2e_overdue\.py' )" ) 2>&1 | tail -25; rc=${PIPESTATUS[0]}
echo "=== [5] exit $rc"; [ $rc -ne 0 ] && rc_all=1
printf "=== [6] %s\n" 'python -c "import ast,glob,sys; ok=set(sys.stdlib_module_names)|{'"'"'app'"'"','"'"'fastapi'"'"','"'"'pydantic'"'"','"'"'uvicorn'"'"','"'"'pytest'"'"','"'"'httpx'"'"','"'"'starlette'"'"'}; fs=glob.glob('"'"'app/**/*.py'"'"',recursive=True)+glob.glob('"'"'tests/**/*.py'"'"',recursive=True); bad=[(f,m) for f in fs for n in ast.walk(ast.parse(open(f).read())) for m in ([a.name.split('"'"'.'"'"')[0] for a in n.names] if isinstance(n,ast.Import) else [n.module.split('"'"'.'"'"')[0]] if isinstance(n,ast.ImportFrom) and n.level==0 else []) if m not in ok]; print('"'"'disallowed imports:'"'"',bad); sys.exit(bool(bad))"'
( python -c "import ast,glob,sys; ok=set(sys.stdlib_module_names)|{'app','fastapi','pydantic','uvicorn','pytest','httpx','starlette'}; fs=glob.glob('app/**/*.py',recursive=True)+glob.glob('tests/**/*.py',recursive=True); bad=[(f,m) for f in fs for n in ast.walk(ast.parse(open(f).read())) for m in ([a.name.split('.')[0] for a in n.names] if isinstance(n,ast.Import) else [n.module.split('.')[0]] if isinstance(n,ast.ImportFrom) and n.level==0 else []) if m not in ok]; print('disallowed imports:',bad); sys.exit(bool(bad))" ) 2>&1 | tail -25; rc=${PIPESTATUS[0]}
echo "=== [6] exit $rc"; [ $rc -ne 0 ] && rc_all=1
printf "=== [7] %s\n" 'python -c "import ast; d=ast.get_docstring(ast.parse(open('"'"'app/main.py'"'"').read())); r=open('"'"'README.md'"'"').read(); assert '"'"'GET /todos/overdue'"'"' in d and '"'"'due_date'"'"' in d, '"'"'docstring'"'"'; assert all(s in r for s in ['"'"'GET /todos/overdue'"'"','"'"'due_date'"'"','"'"'overdue'"'"','"'"'YYYY-MM-DD'"'"','"'"'strictly before'"'"','"'"'UTC'"'"']), '"'"'README'"'"'"'
( python -c "import ast; d=ast.get_docstring(ast.parse(open('app/main.py').read())); r=open('README.md').read(); assert 'GET /todos/overdue' in d and 'due_date' in d, 'docstring'; assert all(s in r for s in ['GET /todos/overdue','due_date','overdue','YYYY-MM-DD','strictly before','UTC']), 'README'" ) 2>&1 | tail -25; rc=${PIPESTATUS[0]}
echo "=== [7] exit $rc"; [ $rc -ne 0 ] && rc_all=1
printf "=== [8] %s\n" '! grep -nE "sleep|datetime\.now|date\.today" tests/test_overdue.py'
( ! grep -nE "sleep|datetime\.now|date\.today" tests/test_overdue.py ) 2>&1 | tail -25; rc=${PIPESTATUS[0]}
echo "=== [8] exit $rc"; [ $rc -ne 0 ] && rc_all=1
printf "=== [9] %s\n" '! grep -nE "extra *=|model_config|ConfigDict" app/main.py'
( ! grep -nE "extra *=|model_config|ConfigDict" app/main.py ) 2>&1 | tail -25; rc=${PIPESTATUS[0]}
echo "=== [9] exit $rc"; [ $rc -ne 0 ] && rc_all=1
printf "=== [10] %s\n" 'python tests/e2e_overdue.py local-all'
( python tests/e2e_overdue.py local-all ) 2>&1 | tail -25; rc=${PIPESTATUS[0]}
echo "=== [10] exit $rc"; [ $rc -ne 0 ] && rc_all=1
printf "=== [11] %s\n" 'PYTHONPATH=. python tests/e2e_overdue.py rollover-local'
( PYTHONPATH=. python tests/e2e_overdue.py rollover-local ) 2>&1 | tail -25; rc=${PIPESTATUS[0]}
echo "=== [11] exit $rc"; [ $rc -ne 0 ] && rc_all=1
echo "ALL_EXIT $rc_all"
