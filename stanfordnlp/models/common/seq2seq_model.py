"""
The full encoder-decoder model, built on top of the base seq2seq modules.
"""

import torch
from torch import nn
import torch.nn.functional as F
import numpy as np

import stanfordnlp.models.common.seq2seq_constant as constant
from stanfordnlp.models.common import utils
from stanfordnlp.models.common.seq2seq_modules import LSTMAttention, LSTMDoubleAttention
from stanfordnlp.models.common.beam import Beam

class Seq2SeqModel(nn.Module):
    """
    A complete encoder-decoder model, with optional attention.
    """
    def __init__(self, args, vocab, emb_matrix=None, use_cuda=False):
        super().__init__()
        self.vocab_size = args['vocab_size']
        self.emb_dim = args['emb_dim']
        self.hidden_dim = args['hidden_dim']
        self.nlayers = args['num_layers'] # encoder layers, decoder layers = 1
        self.emb_dropout = args.get('emb_dropout', 0.0)
        self.dropout = args['dropout']
        self.pad_token = constant.PAD_ID
        self.max_dec_len = args['max_dec_len']
        self.use_cuda = use_cuda
        self.top = args.get('top', 1e10)
        self.args = args
        self.emb_matrix = emb_matrix
        self.vocab = vocab

        print("Building an attentional Seq2Seq model...")
        print("Using a Bi-LSTM encoder")
        self.num_directions = 2
        self.enc_hidden_dim = self.hidden_dim // 2
        self.dec_hidden_dim = self.hidden_dim

        self.use_pos = args.get('pos', False)
        self.pos_first = args.get('pos_first', False)
        self.pos_dim = args.get('pos_dim', 0)
        self.pos_vocab_size = args.get('pos_vocab_size', 0)
        self.pos_dropout = args.get('pos_dropout', 0)
        self.use_feats = args.get('feats', False)
        self.feats_first = args.get('feats_first', False)
        self.feats_vocab_size = args.get('feats_vocab_size', 0)
        self.edit = args.get('edit', False)
        self.num_edit = args.get('num_edit', 0)

        self.use_vabamorf = args.get('vabamorf', False)
        self.vabamorf_first = args.get('vabamorf_first', False)

        self.emb_drop = nn.Dropout(self.emb_dropout)
        self.drop = nn.Dropout(self.dropout)
        self.embedding = nn.Embedding(self.vocab_size, self.emb_dim, self.pad_token)
        self.encoder = nn.LSTM(self.emb_dim, self.enc_hidden_dim, self.nlayers, \
                bidirectional=True, batch_first=True, dropout=self.dropout if self.nlayers > 1 else 0)
        self.decoder = LSTMAttention(self.emb_dim, self.dec_hidden_dim, \
                batch_first=True, attn_type=self.args['attn_type'])
        self.dec2vocab = nn.Linear(self.dec_hidden_dim, self.vocab_size)
        if self.use_pos and self.pos_dim > 0:
            print("Using POS in encoder")
            if self.pos_first:
                print("Putting POS before source in the encoder")
            self.pos_embedding = nn.Embedding(self.pos_vocab_size, self.pos_dim, self.pad_token)
            self.pos_drop = nn.Dropout(self.pos_dropout)
        if self.use_feats and self.pos_dim > 0:
            print("Using FEATS in encoder")
            self.feats_embedding = nn.ModuleList()
            for l in self.vocab['feats'].lens():
                self.feats_embedding.append(nn.Embedding(l, self.pos_dim, self.pad_token))
            self.feats_drop = nn.Dropout(self.pos_dropout)
        if self.use_vabamorf:
            print("Using Vabamorf lemmas in the encoder")
        if self.edit:
            edit_hidden = self.hidden_dim//2
            self.edit_clf = nn.Sequential(
                    nn.Linear(self.hidden_dim, edit_hidden),
                    nn.ReLU(),
                    nn.Linear(edit_hidden, self.num_edit))

        self.SOS_tensor = torch.LongTensor([constant.SOS_ID])
        self.SOS_tensor = self.SOS_tensor.cuda() if self.use_cuda else self.SOS_tensor

        self.init_weights()

    def init_weights(self):
        # initialize embeddings
        init_range = constant.EMB_INIT_RANGE
        if self.emb_matrix is not None:
            if isinstance(self.emb_matrix, np.ndarray):
                self.emb_matrix = torch.from_numpy(self.emb_matrix)
            assert self.emb_matrix.size() == (self.vocab_size, self.emb_dim), \
                    "Input embedding matrix must match size: {} x {}".format(self.vocab_size, self.emb_dim)
            self.embedding.weight.data.copy_(self.emb_matrix)
        else:
            self.embedding.weight.data.uniform_(-init_range, init_range)
        # decide finetuning
        if self.top <= 0:
            print("Do not finetune embedding layer.")
            self.embedding.weight.requires_grad = False
        elif self.top < self.vocab_size:
            print("Finetune top {} embeddings.".format(self.top))
            self.embedding.weight.register_hook(lambda x: utils.keep_partial_grad(x, self.top))
        else:
            print("Finetune all embeddings.")
        # initialize pos embeddings
        if self.use_pos:
            self.pos_embedding.weight.data.uniform_(-init_range, init_range)
        if self.use_feats:
            for fe in self.feats_embedding:
                fe.weight.data.uniform_(-init_range, init_range)

    def cuda(self):
        super().cuda()
        self.use_cuda = True

    def cpu(self):
        super().cpu()
        self.use_cuda = False

    def zero_state(self, inputs):
        batch_size = inputs.size(0)
        h0 = torch.zeros(self.encoder.num_layers*2, batch_size, self.enc_hidden_dim, requires_grad=False)
        c0 = torch.zeros(self.encoder.num_layers*2, batch_size, self.enc_hidden_dim, requires_grad=False)
        if self.use_cuda:
            return h0.cuda(), c0.cuda()
        return h0, c0

    def encode(self, enc_inputs, lens):
        """ Encode source sequence. """
        self.h0, self.c0 = self.zero_state(enc_inputs)

        #print('h0, c0:', self.h0.size(), self.c0.size())

        packed_inputs = nn.utils.rnn.pack_padded_sequence(enc_inputs, lens, batch_first=True)
        packed_h_in, (hn, cn) = self.encoder(packed_inputs, (self.h0, self.c0))
        h_in, _ = nn.utils.rnn.pad_packed_sequence(packed_h_in, batch_first=True)
        #print('hn, cn (original):', hn.size(), cn.size())
        #print('hn[-1], hn[-2]:', hn[-1].size(), hn[-2].size())
        hn = torch.cat((hn[-1], hn[-2]), 1)
        cn = torch.cat((cn[-1], cn[-2]), 1)
        return h_in, (hn, cn)

    def decode(self, dec_inputs, hn, cn, ctx, ctx_mask=None):
        """ Decode a step, based on context encoding and source context states."""
        dec_hidden = (hn, cn)
        h_out, dec_hidden = self.decoder(dec_inputs, dec_hidden, ctx, ctx_mask)

        h_out_reshape = h_out.contiguous().view(h_out.size(0) * h_out.size(1), -1)
        decoder_logits = self.dec2vocab(h_out_reshape)
        decoder_logits = decoder_logits.view(h_out.size(0), h_out.size(1), -1)
        log_probs = self.get_log_prob(decoder_logits)
        return log_probs, dec_hidden

    def forward(self, src, src_mask, tgt_in, pos=None, feats=None, vbf=None, vbf_mask=None):
        # prepare for encoder/decoder
        enc_inputs = self.emb_drop(self.embedding(src))
        dec_inputs = self.emb_drop(self.embedding(tgt_in))
        src_lens = list(src_mask.data.eq(constant.PAD_ID).long().sum(1))
        if self.use_pos:
            # append pos to the end of src sequence
            assert pos is not None
            pos_inputs = self.pos_drop(self.pos_embedding(pos))
            if self.pos_first:
                enc_inputs = torch.cat([pos_inputs.unsqueeze(1), enc_inputs], dim=1)
            else:
                enc_inputs = torch.cat([enc_inputs, pos_inputs.unsqueeze(1)], dim=1)
        if self.use_feats:
            assert feats is not None
            feats_inputs = 0
            for i in range(len(self.vocab['feats'])):
                feats_inputs += self.feats_drop(self.feats_embedding[i](feats[:,:,i]))
            if self.feats_first:
                enc_inputs = torch.cat([feats_inputs, enc_inputs], dim=1)
            else:
                enc_inputs = torch.cat([enc_inputs, feats_inputs], dim=1)
        if self.use_vabamorf:
            assert vbf is not None
            assert vbf_mask is not None
            vabamorf_inputs = self.emb_drop(self.embedding(vbf))
            if self.vabamorf_first:
                enc_inputs = torch.cat([vabamorf_inputs, enc_inputs], dim=1)
            else:
                enc_inputs = torch.cat([enc_inputs, vabamorf_inputs], dim=1)

        h_in, (hn, cn) = self.encode(enc_inputs, src_lens)

        if self.edit:
            edit_logits = self.edit_clf(hn)
        else:
            edit_logits = None
        
        log_probs, _ = self.decode(dec_inputs, hn, cn, h_in, src_mask)
        return log_probs, edit_logits

    def get_log_prob(self, logits):
        logits_reshape = logits.view(-1, self.vocab_size)
        log_probs = F.log_softmax(logits_reshape, dim=1)
        if logits.dim() == 2:
            return log_probs
        return log_probs.view(logits.size(0), logits.size(1), logits.size(2))

    def predict(self, src, src_mask, pos=None, feats=None, vbf=None, vbf_mask=None, beam_size=5):
        """ Predict with beam search. """
        enc_inputs = self.embedding(src)
        batch_size = enc_inputs.size(0)
        src_lens = list(src_mask.data.eq(constant.PAD_ID).long().sum(1))
        if self.use_pos:
            assert pos is not None
            pos_inputs = self.pos_drop(self.pos_embedding(pos))
            if self.pos_first:
                enc_inputs = torch.cat([pos_inputs.unsqueeze(1), enc_inputs], dim=1)
            else:
                enc_inputs = torch.cat([enc_inputs, pos_inputs.unsqueeze(1)], dim=1)
        if self.use_feats:
            assert feats is not None
            feats_inputs = 0
            for i in range(len(self.vocab['feats'])):
                feats_inputs += self.feats_drop(self.feats_embedding[i](feats[:,:,i]))
            if self.feats_first:
                enc_inputs = torch.cat([feats_inputs, enc_inputs], dim=1)
            else:
                enc_inputs = torch.cat([enc_inputs, feats_inputs], dim=1)
        if self.use_vabamorf:
            assert vbf is not None
            assert vbf_mask is not None
            vabamorf_inputs = self.emb_drop(self.embedding(vbf))
            if self.vabamorf_first:
                enc_inputs = torch.cat([vabamorf_inputs, enc_inputs], dim=1)
            else:
                enc_inputs = torch.cat([enc_inputs, vabamorf_inputs], dim=1)

        # (1) encode source
        h_in, (hn, cn) = self.encode(enc_inputs, src_lens)

        if self.edit:
            edit_logits = self.edit_clf(hn)
        else:
            edit_logits = None
        
        # (2) set up beam
        with torch.no_grad():
            h_in = h_in.data.repeat(beam_size, 1, 1) # repeat data for beam search
            src_mask = src_mask.repeat(beam_size, 1)
            # repeat decoder hidden states
            hn = hn.data.repeat(beam_size, 1)
            cn = cn.data.repeat(beam_size, 1)
        beam = [Beam(beam_size, self.use_cuda) for _ in range(batch_size)]

        def update_state(states, idx, positions, beam_size):
            """ Select the states according to back pointers. """
            for e in states:
                br, d = e.size()
                s = e.contiguous().view(beam_size, br // beam_size, d)[:,idx]
                s.data.copy_(s.data.index_select(0, positions))

        # (3) main loop
        for i in range(self.max_dec_len):
            dec_inputs = torch.stack([b.get_current_state() for b in beam]).t().contiguous().view(-1, 1)
            dec_inputs = self.embedding(dec_inputs)
            log_probs, (hn, cn) = self.decode(dec_inputs, hn, cn, h_in, src_mask)
            log_probs = log_probs.view(beam_size, batch_size, -1).transpose(0,1)\
                    .contiguous() # [batch, beam, V]

            # advance each beam
            done = []
            for b in range(batch_size):
                is_done = beam[b].advance(log_probs.data[b])
                if is_done:
                    done += [b]
                # update beam state
                update_state((hn, cn), b, beam[b].get_current_origin(), beam_size)

            if len(done) == batch_size:
                break

        # back trace and find hypothesis
        all_hyp, all_scores = [], []
        for b in range(batch_size):
            scores, ks = beam[b].sort_best()
            all_scores += [scores[0]]
            k = ks[0]
            hyp = beam[b].get_hyp(k)
            hyp = utils.prune_hyp(hyp)
            all_hyp += [hyp]

        return all_hyp, edit_logits


class Seq2SeqModelCombined(Seq2SeqModel):
    """
    A complete encoder-decoder model, with optional attention.
    """
    def __init__(self, args, vocab, emb_matrix=None, use_cuda=False):
        super().__init__(args, vocab, emb_matrix, use_cuda)
        self.vocab_size = args['vocab_size']
        self.emb_dim = args['emb_dim']
        self.hidden_dim = args['hidden_dim']
        self.nlayers = args['num_layers'] # encoder layers, decoder layers = 1
        self.emb_dropout = args.get('emb_dropout', 0.0)
        self.dropout = args['dropout']
        self.pad_token = constant.PAD_ID
        self.max_dec_len = args['max_dec_len']
        self.use_cuda = use_cuda
        self.top = args.get('top', 1e10)
        self.args = args
        self.emb_matrix = emb_matrix
        self.vocab = vocab

        self.use_vabamorf = args.get('vabamorf', False)

        print("Building an attentional Seq2Seq model...")
        print("Using a Bi-LSTM encoder")
        self.num_directions = 2
        self.enc_hidden_dim = self.hidden_dim // 2
        self.dec_hidden_dim = self.hidden_dim

        self.edit = args.get('edit', False)
        self.num_edit = args.get('num_edit', 0)

        self.emb_drop = nn.Dropout(self.emb_dropout)
        self.drop = nn.Dropout(self.dropout)
        self.embedding = nn.Embedding(self.vocab_size, self.emb_dim, self.pad_token)
        self.encoder = nn.LSTM(self.emb_dim, self.enc_hidden_dim, self.nlayers, \
                bidirectional=True, batch_first=True, dropout=self.dropout if self.nlayers > 1 else 0)
        self.lexicon_encoder = nn.LSTM(self.emb_dim, self.enc_hidden_dim, self.nlayers, \
                bidirectional=True, batch_first=True, dropout=self.dropout if self.nlayers > 1 else 0)
        if self.use_vabamorf:
            self.decoder = LSTMDoubleAttention(self.emb_dim, self.dec_hidden_dim, \
                batch_first=True, attn_type=self.args['attn_type'])
            self.hn_linear = nn.Linear(self.enc_hidden_dim * 4, self.enc_hidden_dim * 2)
            self.cn_linear = nn.Linear(self.enc_hidden_dim * 4, self.enc_hidden_dim * 2)
            self.h_in_linear = nn.Linear(self.enc_hidden_dim * 2, self.enc_hidden_dim)
        else:
            self.decoder = LSTMAttention(self.emb_dim, self.dec_hidden_dim, \
                    batch_first=True, attn_type=self.args['attn_type'])
        self.dec2vocab = nn.Linear(self.dec_hidden_dim, self.vocab_size)
        if self.edit:
            edit_hidden = self.dec_hidden_dim//2
            self.edit_clf = nn.Sequential(
                    nn.Linear(self.dec_hidden_dim, edit_hidden),
                    nn.ReLU(),
                    nn.Linear(edit_hidden, self.num_edit))

        self.SOS_tensor = torch.LongTensor([constant.SOS_ID])
        self.SOS_tensor = self.SOS_tensor.cuda() if self.use_cuda else self.SOS_tensor

        self.init_weights()

    def init_weights(self):
        # initialize embeddings
        init_range = constant.EMB_INIT_RANGE
        if self.emb_matrix is not None:
            if isinstance(self.emb_matrix, np.ndarray):
                self.emb_matrix = torch.from_numpy(self.emb_matrix)
            assert self.emb_matrix.size() == (self.vocab_size, self.emb_dim), \
                    "Input embedding matrix must match size: {} x {}".format(self.vocab_size, self.emb_dim)
            self.embedding.weight.data.copy_(self.emb_matrix)
        else:
            self.embedding.weight.data.uniform_(-init_range, init_range)
        # decide finetuning
        if self.top <= 0:
            print("Do not finetune embedding layer.")
            self.embedding.weight.requires_grad = False
        elif self.top < self.vocab_size:
            print("Finetune top {} embeddings.".format(self.top))
            self.embedding.weight.register_hook(lambda x: utils.keep_partial_grad(x, self.top))
        else:
            print("Finetune all embeddings.")

    def encode(self, encoder, enc_inputs, lens):
        """ Encode source sequence. """
        self.h0, self.c0 = self.zero_state(enc_inputs)

        #print('h0, c0:', self.h0.size(), self.c0.size())

        packed_inputs = nn.utils.rnn.pack_padded_sequence(enc_inputs, lens, batch_first=True, enforce_sorted=False)
        #print('enc_inputs:', enc_inputs.size())
        #print('lens:', lens)
        #print('packed_inputs:', packed_inputs.data.size())
        packed_h_in, (hn, cn) = encoder(packed_inputs, (self.h0, self.c0))
        h_in, _ = nn.utils.rnn.pad_packed_sequence(packed_h_in, batch_first=True)
        #print('hn, cn (original):', hn.size(), cn.size())
        #print('hn[-1], hn[-2]:', hn[-1].size(), hn[-2].size())
        hn = torch.cat((hn[-1], hn[-2]), 1)
        cn = torch.cat((cn[-1], cn[-2]), 1)
        return h_in, (hn, cn)

    def decode(self, dec_inputs, hn, cn, src_ctx, lex_ctx=None, ctx_mask=None, lex_mask=None):
        """ Decode a step, based on context encoding and source context states."""
        dec_hidden = (hn, cn)
        if lex_ctx is None:
            h_out, dec_hidden = self.decoder(dec_inputs, dec_hidden, src_ctx, ctx_mask)
        else:
            h_out, dec_hidden = self.decoder(dec_inputs, dec_hidden, src_ctx, lex_ctx, ctx_mask, lex_mask)

        h_out_reshape = h_out.contiguous().view(h_out.size(0) * h_out.size(1), -1)
        decoder_logits = self.dec2vocab(h_out_reshape)
        decoder_logits = decoder_logits.view(h_out.size(0), h_out.size(1), -1)
        log_probs = self.get_log_prob(decoder_logits)
        return log_probs, dec_hidden

    def forward(self, src, src_mask, tgt_in, vbf=None, vbf_mask=None):
        # prepare for encoder/decoder
        enc_inputs = self.emb_drop(self.embedding(src))
        dec_inputs = self.emb_drop(self.embedding(tgt_in))
        src_lens = list(src_mask.data.eq(constant.PAD_ID).long().sum(1))

        h_in, (hn, cn) = self.encode(self.encoder, enc_inputs, src_lens)

        if self.use_vabamorf:
            vbf_inputs = self.emb_drop(self.embedding(vbf))
            vbf_lens = list(vbf_mask.data.eq(constant.PAD_ID).long().sum(1))
            h_in1, (hn1, cn1) = self.encode(self.lexicon_encoder, vbf_inputs, vbf_lens)

            hn = torch.cat((hn, hn1), 1)
            cn = torch.cat((cn, cn1), 1)

            hn = self.hn_linear(hn)
            cn = self.cn_linear(cn)

        #print('h_in:', h_in.size())
        #print('hn:', hn.size())
        #print('cn:', cn.size())
        #print('enc_inputs:', enc_inputs.size())
        #print('vbf_inputs:', vbf_inputs.size())
        #print('src_lens:', src_lens)
        #print('src_mask:', src_mask.data)

        if self.edit:
            edit_logits = self.edit_clf(hn)
        else:
            edit_logits = None
        
        if self.use_vabamorf:
            log_probs, _ = self.decode(dec_inputs, hn, cn, h_in, h_in1, src_mask, vbf_mask)
        else:
            log_probs, _ = self.decode(dec_inputs, hn, cn, h_in, ctx_mask=src_mask)
        return log_probs, edit_logits


    def predict(self, src, src_mask, vbf=None, vbf_mask=None, beam_size=5):
        """ Predict with beam search. """
        enc_inputs = self.embedding(src)
        batch_size = enc_inputs.size(0)
        src_lens = list(src_mask.data.eq(constant.PAD_ID).long().sum(1))
        
        # (1) encode source
        h_in, (hn, cn) = self.encode(self.encoder, enc_inputs, src_lens)

        if self.use_vabamorf:
            vbf_inputs = self.emb_drop(self.embedding(vbf))
            vbf_lens = list(vbf_mask.data.eq(constant.PAD_ID).long().sum(1))
            h_in1, (hn1, cn1) = self.encode(self.lexicon_encoder, vbf_inputs, vbf_lens)

            hn = torch.cat((hn, hn1), 1)
            cn = torch.cat((cn, cn1), 1)

            hn = self.hn_linear(hn)
            cn = self.cn_linear(cn)

        if self.edit:
            edit_logits = self.edit_clf(hn)
        else:
            edit_logits = None
        
        # (2) set up beam
        with torch.no_grad():
            h_in = h_in.data.repeat(beam_size, 1, 1) # repeat data for beam search
            src_mask = src_mask.repeat(beam_size, 1)
            if self.use_vabamorf:
                h_in1 = h_in1.data.repeat(beam_size, 1, 1) # repeat data for beam search
                vbf_mask = vbf_mask.repeat(beam_size, 1)
            # repeat decoder hidden states
            hn = hn.data.repeat(beam_size, 1)
            cn = cn.data.repeat(beam_size, 1)
        beam = [Beam(beam_size, self.use_cuda) for _ in range(batch_size)]

        def update_state(states, idx, positions, beam_size):
            """ Select the states according to back pointers. """
            for e in states:
                br, d = e.size()
                s = e.contiguous().view(beam_size, br // beam_size, d)[:,idx]
                s.data.copy_(s.data.index_select(0, positions))

        # (3) main loop
        for i in range(self.max_dec_len):
            dec_inputs = torch.stack([b.get_current_state() for b in beam]).t().contiguous().view(-1, 1)
            dec_inputs = self.embedding(dec_inputs)
            if self.use_vabamorf:
                log_probs, (hn, cn) = self.decode(dec_inputs, hn, cn, h_in, h_in1, src_mask, vbf_mask)
            else:
                log_probs, (hn, cn) = self.decode(dec_inputs, hn, cn, h_in, ctx_mask=src_mask)
            log_probs = log_probs.view(beam_size, batch_size, -1).transpose(0,1)\
                    .contiguous() # [batch, beam, V]

            # advance each beam
            done = []
            for b in range(batch_size):
                is_done = beam[b].advance(log_probs.data[b])
                if is_done:
                    done += [b]
                # update beam state
                update_state((hn, cn), b, beam[b].get_current_origin(), beam_size)

            if len(done) == batch_size:
                break

        # back trace and find hypothesis
        all_hyp, all_scores = [], []
        for b in range(batch_size):
            scores, ks = beam[b].sort_best()
            all_scores += [scores[0]]
            k = ks[0]
            hyp = beam[b].get_hyp(k)
            hyp = utils.prune_hyp(hyp)
            all_hyp += [hyp]

        return all_hyp, edit_logits
