# coding=utf-8
# @Time    : 2020/7/8
# @Author  : Zhou Yang
# @Email   : zyang@smu.edu.sg
# @File    : attack.py
'''For attacking CodeBERT models'''
import sys
import os
from numpy.core.fromnumeric import sort
from torch.utils.data.dataset import Dataset
sys.path.append('../../../')
sys.path.append('../../../python_parser')

from utils import is_valid_substitue
from run_parser import extract_dataflow
from run_parser import get_identifiers
import logging
import argparse
import enum
from tokenize import tokenize
import warnings
from model import Model
from transformers import (WEIGHTS_NAME, get_linear_schedule_with_warmup, AdamW,
                          RobertaConfig,
                          RobertaForSequenceClassification,
                          RobertaTokenizer)
from torch.utils.data import TensorDataset, SequentialSampler, DataLoader
from torch.utils.data.distributed import DistributedSampler
from run import set_seed
from run import TextDataset
import torch
import torch.nn as nn
from transformers import RobertaForMaskedLM, pipeline
from tqdm import tqdm
import copy
import json
import numpy as np
from run import InputFeatures
import csv
from transformers import (WEIGHTS_NAME, AdamW, get_linear_schedule_with_warmup,
                          BertConfig, BertForMaskedLM, BertTokenizer,
                          GPT2Config, GPT2LMHeadModel, GPT2Tokenizer,
                          OpenAIGPTConfig, OpenAIGPTLMHeadModel, OpenAIGPTTokenizer,
                          RobertaConfig, RobertaForSequenceClassification, RobertaTokenizer,
                          DistilBertConfig, DistilBertForMaskedLM, DistilBertTokenizer)
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
warnings.simplefilter(action='ignore', category=FutureWarning) # Only report warning
MODEL_CLASSES = {
    'gpt2': (GPT2Config, GPT2LMHeadModel, GPT2Tokenizer),
    'openai-gpt': (OpenAIGPTConfig, OpenAIGPTLMHeadModel, OpenAIGPTTokenizer),
    'bert': (BertConfig, BertForMaskedLM, BertTokenizer),
    'roberta': (RobertaConfig, RobertaForSequenceClassification, RobertaTokenizer),
    'distilbert': (DistilBertConfig, DistilBertForMaskedLM, DistilBertTokenizer)
}
logger = logging.getLogger(__name__)

python_keywords = ['import', '', '[', ']', ':', ',', '.', '(', ')', '{', '}', 'not', 'is', '=', "+=", '-=', "<", ">", '+', '-', '*', '/', 'False', 'None', 'True', 'and', 'as', 'assert', 'async', 'await', 'break', 'class', 'continue', 'def', 'del', 'elif', 'else', 'except', 'finally', 'for', 'from', 'global', 'if', 'import', 'in', 'is', 'lambda', 'nonlocal', 'not', 'or', 'pass', 'raise', 'return', 'try', 'while', 'with', 'yield']



class CodeDataset(Dataset):
    def __init__(self, examples, args):
        self.examples = examples
        self.args=args
    
    def __len__(self):
        return len(self.examples)

    def __getitem__(self, item):
        #calculate graph-guided masked function
        attn_mask=np.zeros((self.args.code_length+self.args.data_flow_length,
                            self.args.code_length+self.args.data_flow_length),dtype=np.bool)
        #calculate begin index of node and max length of input
        
        node_index=sum([i>1 for i in self.examples[item].position_idx])
        max_length=sum([i!=1 for i in self.examples[item].position_idx])
        #sequence can attend to sequence
        attn_mask[:node_index,:node_index]=True
        #special tokens attend to all tokens
        for idx,i in enumerate(self.examples[item].input_ids):
            if i in [0,2]:
                attn_mask[idx,:max_length]=True
        #nodes attend to code tokens that are identified from
        for idx,(a,b) in enumerate(self.examples[item].dfg_to_code):
            if a<node_index and b<node_index:
                attn_mask[idx+node_index,a:b]=True
                attn_mask[a:b,idx+node_index]=True
        #nodes attend to adjacent nodes 
        for idx,nodes in enumerate(self.examples[item].dfg_to_dfg):
            for a in nodes:
                if a+node_index<len(self.examples[item].position_idx):
                    attn_mask[idx+node_index,a+node_index]=True
              
        return (torch.tensor(self.examples[item].input_ids),
              torch.tensor(attn_mask),
              torch.tensor(self.examples[item].position_idx),
              torch.tensor(self.examples[item].label))

def _tokenize(seq, tokenizer):
    seq = seq.replace('\n', '').lower()
    words = seq.split(' ')

    sub_words = []
    keys = []
    index = 0
    for word in words:
        # 并非直接tokenize这句话，而是tokenize了每个splited words.
        sub = tokenizer.tokenize(word)
        sub_words += sub
        keys.append([index, index + len(sub)])
        # 将subwords对齐
        index += len(sub)

    return words, sub_words, keys

def get_identifier_posistions_from_code(words_list: list, variable_names: list) -> dict:
    '''
    给定一串代码，以及variable的变量名，如: a
    返回这串代码中这些变量名对应的位置.
    '''
    positions = {}
    for name in variable_names:
        for index, token in enumerate(words_list):
            if name == token:
                try:
                    positions[name].append(index)
                except:
                    positions[name] = [index]

    return positions

def get_bpe_substitues(substitutes, tokenizer, mlm_model):
    '''
    得到substitues
    '''
    # substitutes L, k

    substitutes = substitutes[0:12, 0:4] # maximum BPE candidates

    # find all possible candidates 

    all_substitutes = []
    for i in range(substitutes.size(0)):
        if len(all_substitutes) == 0:
            lev_i = substitutes[i]
            all_substitutes = [[int(c)] for c in lev_i]
        else:
            lev_i = []
            for all_sub in all_substitutes[:24]: # 去掉不用的计算.
                for j in substitutes[i]:
                    lev_i.append(all_sub + [int(j)])
            all_substitutes = lev_i
    # all substitutes  list of list of token-id (all candidates)
    c_loss = nn.CrossEntropyLoss(reduction='none')
    word_list = []
    # all_substitutes = all_substitutes[:24]
    all_substitutes = torch.tensor(all_substitutes) # [ N, L ]
    all_substitutes = all_substitutes[:24].to('cuda')
    # 不是，这个总共不会超过24... 那之前生成那么多也没用....
    # print(substitutes.size(), all_substitutes.size())
    N, L = all_substitutes.size()
    word_predictions = mlm_model(all_substitutes)[0] # N L vocab-size
    ppl = c_loss(word_predictions.view(N*L, -1), all_substitutes.view(-1)) # [ N*L ] 
    ppl = torch.exp(torch.mean(ppl.view(N, L), dim=-1)) # N  
    _, word_list = torch.sort(ppl)
    word_list = [all_substitutes[i] for i in word_list]
    final_words = []
    for word in word_list:
        tokens = [tokenizer._convert_id_to_token(int(i)) for i in word]
        text = tokenizer.convert_tokens_to_string(tokens)
        final_words.append(text)
    return final_words

def get_substitues(substitutes, tokenizer, mlm_model, use_bpe, substitutes_score=None, threshold=3.0):
    '''
    将生成的substitued subwords转化为words
    '''
    # substitues L,k
    # from this matrix to recover a word
    words = []
    sub_len, k = substitutes.size()  # sub-len, k

    if sub_len == 0:
        # 比如空格对应的subwords就是[a,a]，长度为0
        return words
        
    elif sub_len == 1:
        # subwords就是本身
        for (i,j) in zip(substitutes[0], substitutes_score[0]):
            if threshold != 0 and j < threshold:
                break
            words.append(tokenizer._convert_id_to_token(int(i)))
            # 将id转为token.
    else:
        # word被分解成了多个subwords
        if use_bpe == 1:
            words = get_bpe_substitues(substitutes, tokenizer, mlm_model)
        else:
            return words
    return words


def get_masked_code_by_position(tokens: list, positions: dict):
    '''
    给定一段文本，以及需要被mask的位置,返回一组masked后的text
    Example:
        tokens: [a,b,c]
        positions: [0,2]
        Return:
            [<mask>, b, c]
            [a, b, <mask>]
    '''
    masked_token_list = []
    replace_token_positions = []
    for variable_name in positions.keys():
        for pos in positions[variable_name]:
            masked_token_list.append(tokens[0:pos] + ['<unk>'] + tokens[pos + 1:])
            replace_token_positions.append(pos)
    
    return masked_token_list, replace_token_positions



def get_results(dataset, model, batch_size):
    '''
    给定example和tgt model，返回预测的label和probability
    '''


    eval_sampler = SequentialSampler(dataset)
    eval_dataloader = DataLoader(dataset, sampler=eval_sampler, batch_size=batch_size,num_workers=0,pin_memory=True)

    ## Evaluate Model

    eval_loss = 0.0
    nb_eval_steps = 0
    model.eval()
    logits=[] 
    labels=[]
    for batch in eval_dataloader:
        inputs_ids = batch[0].to("cuda")       
        attn_mask = batch[1].to("cuda") 
        position_idx = batch[2].to("cuda") 
        label=batch[3].to("cuda") 
        with torch.no_grad():
            lm_loss,logit = model(inputs_ids, attn_mask, position_idx, label)
            # 调用这个模型. 重写了反前向传播模型.
            eval_loss += lm_loss.mean().item()
            logits.append(logit.cpu().numpy())
            labels.append(label.cpu().numpy())
            

        nb_eval_steps += 1
    logits=np.concatenate(logits,0)
    labels=np.concatenate(labels,0)

    probs = [[1 - prob[0], prob[0]] for prob in logits]
    pred_labels = [1 if label else 0 for label in logits[:,0]>0.5]

    return probs, pred_labels

def convert_code_to_features(code, tokenizer, label, args):
    # 这里要被修改..
    dfg, index_table, code_tokens = extract_dataflow(code, "c")
    code_tokens=[tokenizer.tokenize('@ '+x)[1:] if idx!=0 else tokenizer.tokenize(x) for idx,x in enumerate(code_tokens)]
    ori2cur_pos={}
    ori2cur_pos[-1]=(0,0)
    for i in range(len(code_tokens)):
        ori2cur_pos[i]=(ori2cur_pos[i-1][1],ori2cur_pos[i-1][1]+len(code_tokens[i]))    
    code_tokens=[y for x in code_tokens for y in x]

    code_tokens=code_tokens[:args.code_length+args.data_flow_length-2-min(len(dfg),args.data_flow_length)]
    source_tokens =[tokenizer.cls_token]+code_tokens+[tokenizer.sep_token]
    source_ids =  tokenizer.convert_tokens_to_ids(source_tokens)
    position_idx = [i+tokenizer.pad_token_id + 1 for i in range(len(source_tokens))]
    dfg = dfg[:args.code_length+args.data_flow_length-len(source_tokens)]
    source_tokens += [x[0] for x in dfg]
    position_idx+=[0 for x in dfg]
    source_ids+=[tokenizer.unk_token_id for x in dfg]
    padding_length=args.code_length+args.data_flow_length-len(source_ids)
    position_idx+=[tokenizer.pad_token_id]*padding_length
    source_ids+=[tokenizer.pad_token_id]*padding_length

    reverse_index={}
    for idx,x in enumerate(dfg):
        reverse_index[x[1]]=idx
    for idx,x in enumerate(dfg):
        dfg[idx]=x[:-1]+([reverse_index[i] for i in x[-1] if i in reverse_index],)    
    dfg_to_dfg=[x[-1] for x in dfg]
    dfg_to_code=[ori2cur_pos[x[1]] for x in dfg]
    length=len([tokenizer.cls_token])
    dfg_to_code=[(x[0]+length,x[1]+length) for x in dfg_to_code]
    
    return InputFeatures(source_tokens, source_ids, position_idx, dfg_to_code, dfg_to_dfg, 0, label)

def get_importance_score(args, example, code, words_list: list, sub_words: list, variable_names: list, tgt_model, tokenizer, label_list, batch_size=16, max_length=512, model_type='classification'):
    '''
    计算importance score
    '''
    # label: example[1] tensor(1)
    # 1. 过滤掉所有的keywords.
    positions = get_identifier_posistions_from_code(words_list, variable_names)
    # 需要注意大小写.
    if len(positions) == 0:
        ## 没有提取出可以mutate的position
        return None, None, None

    new_example = []

    # 2. 得到Masked_tokens
    masked_token_list, replace_token_positions = get_masked_code_by_position(words_list, positions)
    # replace_token_positions 表示着，哪一个位置的token被替换了.


    for index, tokens in enumerate([words_list] + masked_token_list):
        new_code = ' '.join(tokens)
        new_feature = convert_code_to_features(new_code, tokenizer, example[3].item(), args)
        new_example.append(new_feature)
    new_dataset = CodeDataset(new_example, args)
    # 3. 将他们转化成features
    logits, preds = get_results(new_dataset, tgt_model, args.eval_batch_size)
    orig_probs = logits[0]
    orig_label = preds[0]
    # 第一个是original code的数据.
    
    orig_prob = max(orig_probs)
    # predicted label对应的probability

    importance_score = []
    for prob in logits[1:]:
        importance_score.append(orig_prob - prob[orig_label])

    return importance_score, replace_token_positions, positions



def attack(args, example, code, codebert_tgt, tokenizer_tgt, codebert_mlm, tokenizer_mlm, use_bpe, threshold_pred_score):
    '''
    return
        original program: code
        program length: prog_length
        adversar program: adv_program
        true label: true_label
        original prediction: orig_label
        adversarial prediction: temp_label
        is_attack_success: is_success
        extracted variables: variable_names
        importance score of variables: names_to_importance_score
        number of changed variables: nb_changed_var
        number of changed positions: nb_changed_pos
        substitues for variables: replaced_words
    '''
        # 先得到tgt_model针对原始Example的预测信息.

    logits, preds = get_results([example], codebert_tgt, args.eval_batch_size)
    orig_prob = logits[0]
    orig_label = preds[0]
    current_prob = max(orig_prob)

    true_label = example[3].item()
    adv_code = ''
    temp_label = None



    identifiers, code_tokens = get_identifiers(code, 'c')
    prog_length = len(code_tokens)


    processed_code = " ".join(code_tokens)
    
    words, sub_words, keys = _tokenize(processed_code, tokenizer_mlm)
    # 这里经过了小写处理..


    variable_names = []
    for name in identifiers:
        if ' ' in name[0].strip() or name[0].lower() in variable_names:
            continue
        variable_names.append(name[0].lower())

    print("Number of identifiers extracted: ", len(variable_names))
    if not orig_label == true_label:
        # 说明原来就是错的
        is_success = -4
        return code, prog_length, adv_code, true_label, orig_label, temp_label, is_success, variable_names, None, None, None, None
        
    if len(variable_names) == 0:
        # 没有提取到identifier，直接退出
        is_success = -3
        return code, prog_length, adv_code, true_label, orig_label, temp_label, is_success, variable_names, None, None, None, None

    sub_words = [tokenizer_tgt.cls_token] + sub_words[:args.block_size - 2] + [tokenizer_tgt.sep_token]
    # 如果长度超了，就截断；这里的block_size是CodeBERT能接受的输入长度
    input_ids_ = torch.tensor([tokenizer_mlm.convert_tokens_to_ids(sub_words)])
    word_predictions = codebert_mlm(input_ids_.to('cuda'))[0].squeeze()  # seq-len(sub) vocab
    word_pred_scores_all, word_predictions = torch.topk(word_predictions, 30, -1)  # seq-len k
    # 得到前k个结果.

    word_predictions = word_predictions[1:len(sub_words) + 1, :]
    word_pred_scores_all = word_pred_scores_all[1:len(sub_words) + 1, :]
    # 只取subwords的部分，忽略首尾的预测结果.

    # 计算importance_score.

    importance_score, replace_token_positions, names_positions_dict = get_importance_score(args, example, 
                                            code,
                                            words,
                                            sub_words,
                                            variable_names,
                                            codebert_tgt, 
                                            tokenizer_tgt, 
                                            [0,1], 
                                            batch_size=args.eval_batch_size, 
                                            max_length=args.block_size, 
                                            model_type='classification')

    assert(len(importance_score) == len(replace_token_positions))

    token_pos_to_score_pos = {}

    for i, token_pos in enumerate(replace_token_positions):
        token_pos_to_score_pos[token_pos] = i
    # 重新计算Importance score，将所有出现的位置加起来（而不是取平均）.
    names_to_importance_score = {}

    for name in names_positions_dict.keys():
        total_score = 0.0
        positions = names_positions_dict[name]
        for token_pos in positions:
            # 这个token在code中对应的位置
            # importance_score中的位置：token_pos_to_score_pos[token_pos]
            total_score += importance_score[token_pos_to_score_pos[token_pos]]
        
        names_to_importance_score[name] = total_score

    sorted_list_of_names = sorted(names_to_importance_score.items(), key=lambda x: x[1], reverse=True)
    # 根据importance_score进行排序

    final_words = copy.deepcopy(words)
    
    nb_changed_var = 0 # 表示被修改的variable数量
    nb_changed_pos = 0
    is_success = -1
    replaced_words = {}

    for name_and_score in sorted_list_of_names:
        tgt_word = name_and_score[0]
        tgt_positions = names_positions_dict[tgt_word] # 在words中对应的位置
        if tgt_word in python_keywords:
            # 如果在filter_words中就不修改
            continue   

        ## 得到substitues
        all_substitues = []
        for one_pos in tgt_positions:
            ## 一个变量名会出现很多次
            substitutes = word_predictions[keys[one_pos][0]:keys[one_pos][1]]  # L, k
            word_pred_scores = word_pred_scores_all[keys[one_pos][0]:keys[one_pos][1]]

            substitutes = get_substitues(substitutes, 
                                        tokenizer_mlm, 
                                        codebert_mlm, 
                                        use_bpe, 
                                        word_pred_scores, 
                                        threshold_pred_score)
            all_substitues += substitutes
        all_substitues = set(all_substitues)
        # 得到了所有位置的substitue，并使用set来去重

        most_gap = 0.0
        candidate = None
        replace_examples = []

        substitute_list = []
        # 依次记录了被加进来的substitue
        # 即，每个temp_replace对应的substitue.
        for substitute_ in all_substitues:

            substitute = substitute_.strip()
            # FIX: 有些substitue的开头或者末尾会产生空格
            # 这些头部和尾部的空格在拼接的时候并不影响，但是因为下面的第4个if语句会被跳过
            # 这导致了部分mutants为空，而引发了runtime error

            if not is_valid_substitue(substitute, tgt_word):
                continue

            
            temp_replace = copy.deepcopy(final_words)
            for one_pos in tgt_positions:
                temp_replace[one_pos] = substitute
            
            substitute_list.append(substitute)
            # 记录了替换的顺序

            # 需要将几个位置都替换成sustitue_
            temp_code = " ".join(temp_replace)
                                            
            new_feature = convert_code_to_features(temp_code, tokenizer_tgt, example[3].item(), args)
            replace_examples.append(new_feature)
        if len(replace_examples) == 0:
            # 并没有生成新的mutants，直接跳去下一个token
            continue
        new_dataset = CodeDataset(replace_examples, args)
            # 3. 将他们转化成features
        logits, preds = get_results(new_dataset, codebert_tgt, args.eval_batch_size)
        assert(len(logits) == len(substitute_list))


        for index, temp_prob in enumerate(logits):
            temp_label = preds[index]
            if temp_label != orig_label:
                # 如果label改变了，说明这个mutant攻击成功
                is_success = 1
                nb_changed_var += 1
                nb_changed_pos += len(names_positions_dict[tgt_word])
                candidate = substitute_list[index]
                replaced_words[tgt_word] = candidate
                for one_pos in tgt_positions:
                    final_words[one_pos] = candidate
                adv_code = " ".join(final_words)

                return code, prog_length, adv_code, true_label, orig_label, temp_label, is_success, variable_names, names_to_importance_score, nb_changed_var, nb_changed_pos, replaced_words
            else:
                # 如果没有攻击成功，我们看probability的修改
                gap = current_prob - temp_prob[temp_label]
                # 并选择那个最大的gap.
                if gap > most_gap:
                    most_gap = gap
                    candidate = substitute_list[index]
    
        if most_gap > 0:
            # 如果most_gap > 0，说明有mutant可以让prob减少
            nb_changed_var += 1
            nb_changed_pos += len(names_positions_dict[tgt_word])
            current_prob = current_prob - most_gap
            for one_pos in tgt_positions:
                final_words[one_pos] = candidate
            replaced_words[tgt_word] = candidate
        
        adv_code = " ".join(final_words)
    '''
    return
        original program: code
        program length: prog_length
        adversar program: adv_code
        true label: true_label
        original prediction: orig_label
        adversarial prediction: temp_label
        is_attack_success: is_success
        extracted variables: variable_names
        importance score of variables: names_to_importance_score
        number of changed variables: nb_changed_var
        number of changed positions: nb_changed_pos
        substitues for variables: replaced_words
    '''
    return code, prog_length, adv_code, true_label, orig_label, temp_label, is_success, variable_names, names_to_importance_score, nb_changed_var, nb_changed_pos, replaced_words


def main():
    parser = argparse.ArgumentParser()

    ## Required parameters
    parser.add_argument("--train_data_file", default=None, type=str, required=True,
                        help="The input training data file (a text file).")
    parser.add_argument("--output_dir", default=None, type=str, required=True,
                        help="The output directory where the model predictions and checkpoints will be written.")

    ## Other parameters
    parser.add_argument("--eval_data_file", default=None, type=str,
                        help="An optional input evaluation data file to evaluate the perplexity on (a text file).")
    parser.add_argument("--test_data_file", default=None, type=str,
                        help="An optional input evaluation data file to evaluate the perplexity on (a text file).")
                    
    parser.add_argument("--model_type", default="bert", type=str,
                        help="The model architecture to be fine-tuned.")
    parser.add_argument("--model_name_or_path", default=None, type=str,
                        help="The model checkpoint for weights initialization.")

    parser.add_argument("--mlm", action='store_true',
                        help="Train with masked-language modeling loss instead of language modeling.")
    parser.add_argument("--mlm_probability", type=float, default=0.15,
                        help="Ratio of tokens to mask for masked language modeling loss")

    parser.add_argument("--config_name", default="", type=str,
                        help="Optional pretrained config name or path if not the same as model_name_or_path")
    parser.add_argument("--tokenizer_name", default="", type=str,
                        help="Optional pretrained tokenizer name or path if not the same as model_name_or_path")
    parser.add_argument("--cache_dir", default="", type=str,
                        help="Optional directory to store the pre-trained models downloaded from s3 (instread of the default one)")
    parser.add_argument("--block_size", default=-1, type=int,
                        help="Optional input sequence length after tokenization."
                             "The training dataset will be truncated in block of this size for training."
                             "Default to the model max input length for single sentence inputs (take into account special tokens).")
    parser.add_argument("--do_train", action='store_true',
                        help="Whether to run training.")
    parser.add_argument("--do_eval", action='store_true',
                        help="Whether to run eval on the dev set.")
    parser.add_argument("--do_test", action='store_true',
                        help="Whether to run eval on the dev set.")    
    parser.add_argument("--evaluate_during_training", action='store_true',
                        help="Run evaluation during training at each logging step.")
    parser.add_argument("--do_lower_case", action='store_true',
                        help="Set this flag if you are using an uncased model.")
    parser.add_argument("--data_flow_length", default=64, type=int,
                        help="Optional Data Flow input sequence length after tokenization.") 
    parser.add_argument("--train_batch_size", default=4, type=int,
                        help="Batch size per GPU/CPU for training.")
    parser.add_argument("--eval_batch_size", default=4, type=int,
                        help="Batch size per GPU/CPU for evaluation.")
    parser.add_argument('--gradient_accumulation_steps', type=int, default=1,
                        help="Number of updates steps to accumulate before performing a backward/update pass.")
    parser.add_argument("--learning_rate", default=5e-5, type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument("--weight_decay", default=0.0, type=float,
                        help="Weight deay if we apply some.")
    parser.add_argument("--adam_epsilon", default=1e-8, type=float,
                        help="Epsilon for Adam optimizer.")
    parser.add_argument("--max_grad_norm", default=1.0, type=float,
                        help="Max gradient norm.")
    parser.add_argument("--num_train_epochs", default=1.0, type=float,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--max_steps", default=-1, type=int,
                        help="If > 0: set total number of training steps to perform. Override num_train_epochs.")
    parser.add_argument("--warmup_steps", default=0, type=int,
                        help="Linear warmup over warmup_steps.")
    parser.add_argument("--code_length", default=256, type=int,
                        help="Optional Code input sequence length after tokenization.") 
    parser.add_argument('--logging_steps', type=int, default=50,
                        help="Log every X updates steps.")
    parser.add_argument('--save_steps', type=int, default=50,
                        help="Save checkpoint every X updates steps.")
    parser.add_argument('--save_total_limit', type=int, default=None,
                        help='Limit the total amount of checkpoints, delete the older checkpoints in the output_dir, does not delete by default')
    parser.add_argument("--eval_all_checkpoints", action='store_true',
                        help="Evaluate all checkpoints starting with the same prefix as model_name_or_path ending and ending with step number")
    parser.add_argument("--no_cuda", action='store_true',
                        help="Avoid using CUDA when available")
    parser.add_argument('--overwrite_output_dir', action='store_true',
                        help="Overwrite the content of the output directory")
    parser.add_argument('--overwrite_cache', action='store_true',
                        help="Overwrite the cached training and evaluation sets")
    parser.add_argument('--seed', type=int, default=42,
                        help="random seed for initialization")
    parser.add_argument('--epoch', type=int, default=42,
                        help="random seed for initialization")
    parser.add_argument('--fp16', action='store_true',
                        help="Whether to use 16-bit (mixed) precision (through NVIDIA apex) instead of 32-bit")
    parser.add_argument('--fp16_opt_level', type=str, default='O1',
                        help="For fp16: Apex AMP optimization level selected in ['O0', 'O1', 'O2', and 'O3']."
                             "See details at https://nvidia.github.io/apex/amp.html")
    parser.add_argument("--local_rank", type=int, default=-1,
                        help="For distributed training: local_rank")
    parser.add_argument('--server_ip', type=str, default='', help="For distant debugging.")
    parser.add_argument('--server_port', type=str, default='', help="For distant debugging.")


    args = parser.parse_args()


    args.device = torch.device("cuda")
    # Set seed
    set_seed(args.seed)


    args.start_epoch = 0
    args.start_step = 0


    ## Load Target Model
    checkpoint_last = os.path.join(args.output_dir, 'checkpoint-last') # 读取model的路径
    if os.path.exists(checkpoint_last) and os.listdir(checkpoint_last):
        # 如果路径存在且有内容，则从checkpoint load模型
        args.model_name_or_path = os.path.join(checkpoint_last, 'pytorch_model.bin')
        args.config_name = os.path.join(checkpoint_last, 'config.json')
        idx_file = os.path.join(checkpoint_last, 'idx_file.txt')
        with open(idx_file, encoding='utf-8') as idxf:
            args.start_epoch = int(idxf.readlines()[0].strip()) + 1

        step_file = os.path.join(checkpoint_last, 'step_file.txt')
        if os.path.exists(step_file):
            with open(step_file, encoding='utf-8') as stepf:
                args.start_step = int(stepf.readlines()[0].strip())
        logger.info("reload model from {}, resume from {} epoch".format(checkpoint_last, args.start_epoch))


    config_class, model_class, tokenizer_class = MODEL_CLASSES[args.model_type]
    config = config_class.from_pretrained(args.config_name if args.config_name else args.model_name_or_path,
                                          cache_dir=args.cache_dir if args.cache_dir else None)
    config.num_labels=1 # 只有一个label?
    tokenizer = tokenizer_class.from_pretrained(args.tokenizer_name,
                                                do_lower_case=args.do_lower_case,
                                                cache_dir=args.cache_dir if args.cache_dir else None)
    if args.block_size <= 0:
        args.block_size = tokenizer.max_len_single_sentence  # Our input block size will be the max possible for the model
    args.block_size = min(args.block_size, tokenizer.max_len_single_sentence)
    if args.model_name_or_path:
        model = model_class.from_pretrained(args.model_name_or_path,
                                            from_tf=bool('.ckpt' in args.model_name_or_path),
                                            config=config,
                                            cache_dir=args.cache_dir if args.cache_dir else None)    
    else:
        model = model_class(config)

    model = Model(model,config,tokenizer,args)


    checkpoint_prefix = 'checkpoint-best-acc/model.bin'
    output_dir = os.path.join(args.output_dir, '{}'.format(checkpoint_prefix))  
    model.load_state_dict(torch.load(output_dir))      
    model.to(args.device)
    # 会是因为模型不同吗？我看evaluate的时候模型是重新导入的.


    ## Load CodeBERT (MLM) model
    codebert_mlm = RobertaForMaskedLM.from_pretrained("microsoft/graphcodebert-base")
    tokenizer_mlm = RobertaTokenizer.from_pretrained("microsoft/graphcodebert-base")
    codebert_mlm.to('cuda') 

    ## Load Dataset
    eval_dataset = TextDataset(tokenizer, args,args.eval_data_file)

    source_codes = []
    with open(args.eval_data_file) as f:
        for line in f:
            js=json.loads(line.strip())
            code = ' '.join(js['func'].split())
            source_codes.append(code)
    assert(len(source_codes) == len(eval_dataset))

    # 现在要尝试计算importance_score了.
    success_attack = 0
    total_cnt = 0
    f = open('./attack_result.csv', 'w')
    writer = csv.writer(f)
    # write table head.
    writer.writerow(["Original Code", 
                    "Program Length", 
                    "Adversarial Code", 
                    "True Label", 
                    "Original Prediction", 
                    "Adv Prediction", 
                    "Is Success", 
                    "Extracted Names",
                    "Importance Score",
                    "No. Changed Names",
                    "No. Changed Tokens",
                    "Replaced Names"])
    for index, example in enumerate(eval_dataset):
        code = source_codes[index]
        code, prog_length, adv_code, true_label, orig_label, temp_label, is_success, variable_names, names_to_importance_score, nb_changed_var, nb_changed_pos, replaced_words = attack(args, example, code, model, tokenizer, codebert_mlm, tokenizer_mlm, use_bpe=1, threshold_pred_score=0)


        score_info = ''
        if names_to_importance_score is not None:
            for key in names_to_importance_score.keys():
                score_info += key + ':' + str(names_to_importance_score[key]) + ','

        replace_info = ''
        if replaced_words is not None:
            for key in replaced_words.keys():
                replace_info += key + ':' + replaced_words[key] + ','

        writer.writerow([code, 
                        prog_length, 
                        adv_code, 
                        true_label, 
                        orig_label, 
                        temp_label, 
                        is_success, 
                        ",".join(variable_names),
                        score_info,
                        nb_changed_var,
                        nb_changed_pos,
                        replace_info])
        
        
        if is_success >= -1 :
            # 如果原来正确
            total_cnt += 1
        if is_success == 1:
            success_attack += 1
        
        if total_cnt == 0:
            continue
        print("Success rate: ", 1.0 * success_attack / total_cnt)
        print(success_attack)
        print(total_cnt)
    

if __name__ == '__main__':
    main()
