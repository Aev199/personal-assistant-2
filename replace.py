import glob

for filepath in glob.glob('bot/**/*.py', recursive=True):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # We want to replace f"❌ Ошибка: {h(str(e))}"
    # with f"❌ Ошибка загрузки. Дли фикса: {h(str(e))}"
    target = 'f"❌ Ошибка: {h(str(e))}"'
    replacement = 'f"❌ Ошибка загрузки. Для фикса: {h(str(e))}"'
    
    if target in content:
        print(f"Replacing in {filepath}")
        new_content = content.replace(target, replacement)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(new_content)
