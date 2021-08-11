import os

def preprocess_gcjpy(split_pos):
    '''
    预处理文件.
    需要将结果分成train和valid
    '''
    data_name = "gcjpy"
    folder = os.path.join('./data_folder', data_name)
    output_dir = os.path.join('./data_folder', "processed_" + data_name)
    if not os.path.exists(output_dir):
        os.mkdir(output_dir)
    authors = os.listdir(folder)

    with open(os.path.join(output_dir, "classes.txt"), 'w') as f:
        for index, name in enumerate(authors):
            f.write(str(index) + '\t' + name + '\n')



    train_example = []
    valid_example = []
    for index, name in enumerate(authors):
        files = os.listdir(os.path.join(folder, name))
        tmp_example = []
        for file_name in files:
            with open(os.path.join(folder, name, file_name)) as code_file:
                content = code_file.read()
                new_content = content.replace('\n', ' ') + ' <CODESPLIT> ' + str(index) + '\n'
                tmp_example.append(new_content)
        train_example += tmp_example[0:split_pos]
        valid_example += tmp_example[split_pos:]

            # 8 for train and 2 for validation

    with open(os.path.join(output_dir, "train.txt"), 'w') as f:
        for example in train_example:
            f.write(example)
    
    with open(os.path.join(output_dir, "valid.txt"), 'w') as f:
        for example in valid_example:
            f.write(example)

def preprocess_java40(split_portion = 0.8):
    '''
    预处理文件.
    需要将结果分成train和valid
    '''
    data_name = "java40"
    folder = os.path.join('./data_folder', data_name)
    output_dir = os.path.join('./data_folder', "processed_" + data_name)
    if not os.path.exists(output_dir):
        os.mkdir(output_dir)
    authors = os.listdir(folder)

    with open(os.path.join(output_dir, "classes.txt"), 'w') as f:
        for index, name in enumerate(authors):
            if name[0] == '.':
                continue
            f.write(str(index) + '\t' + name + '\n')



    train_example = []
    valid_example = []
    for index, name in enumerate(authors):
        if name[0] == '.':
            continue
        repos = os.listdir(os.path.join(folder, name))
        for repo in repos:
            files = os.listdir(os.path.join(folder, name, repo))
            tmp_example = []
            for file_name in files:
                with open(os.path.join(folder, name, repo, file_name), encoding="utf8", errors='ignore') as code_file:
                    content = code_file.read()
                    new_content = content.replace('\n', ' ') + ' <CODESPLIT> ' + str(index) + '\n'
                    tmp_example.append(new_content)
            split_pos = int(len(tmp_example) * split_portion)
            train_example += tmp_example[0:split_pos]
            valid_example += tmp_example[split_pos:]

            # 8 for train and 2 for validation

    with open(os.path.join(output_dir, "train.txt"), 'w') as f:
        for example in train_example:
            f.write(example)
    
    with open(os.path.join(output_dir, "valid.txt"), 'w') as f:
        for example in valid_example:
            f.write(example)


if __name__ == "__main__":
    preprocess_gcjpy(8)
    # preprocess_java40(0.8)