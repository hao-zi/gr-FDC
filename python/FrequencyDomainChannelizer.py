#!/usr/bin/env python
# -*- coding: utf-8 -*-
# 
# Copyright 2018 Gereon Such.
# 
# This is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3, or (at your option)
# any later version.
# 
# This software is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with this software; see the file COPYING.  If not, write to
# the Free Software Foundation, Inc., 51 Franklin Street,
# Boston, MA 02110-1301, USA.
# 

from gnuradio import gr
from gnuradio import blocks
from gnuradio import fft
from FDC import overlap_save, phase_shifting_windowing_vcc, vector_cut_vxx, activity_detection_channelizer_vcm, PowerActivationChannel
import numpy
import pmt
import os, time


class FREQMODE:
    normalized, basebandfs, centerfreqfs = range(3)

class VERBOSEMODE:
    NOLOG, LOGTOCONSOLE, LOGTOFILE = range(3)

def nextpow2(k):
    if k<1:
        raise ValueError('Cannot evaluate next power 2 of {}'.format(k))
    return 2**int(numpy.ceil(numpy.log2(k)))

class FrequencyDomainChannelizer(gr.hier_block2):
    """
    docstring for block FrequencyDomainChannelizer
    """
    def __init__(self, inptype, inpveclen, blocksize, relinvovl, 
                 throughput_channels, 
                 activity_controlled_channels, 
                 act_contr_threshold, 
                 fs, centerfrequency, freqmode, 
                 windowtype, 
                 msgoutput, fileoutput, outputpath, 
                 threaded, 
                 
                 activity_detection_segments, act_det_threshold, minchandist, 
                 act_det_deactivation_delay, minchanflankpuffer, verbose,
                 pow_act_deactivation_delay,
                 pow_act_maxblocks, act_det_maxblocks,
                 
                 debug):
        #from GRC
        #$type.size, $blocksize, $relinvovl, $throughput_channels, $activity_controlled_channels, $act_contr_threshold, $fs, $centerfrequency, $freqmode, $windowtype, $msgoutput, $fileoutput, $outputpath, $threaded, $activity_detection_segments, $act_det_threshold, $minchandist, $act_det_deactivation_delay, $minchanflankpuffer, $verbose, $pow_act_deactivation_delay, $pow_act_maxblocks, $act_det_maxblocks
        
        self.verbose=int(verbose)
        self.itemsize=inptype
        self.debug=bool(debug)
        
        #all frequencies are stored normalized, thus we need conversion
        #maybe further distinction to real signals(though FFT is complex)
        self.get_freq=lambda f: f
        self.set_freq=lambda f: f
        self.get_bw=lambda bw: bw
        self.set_bw=lambda bw: bw
        
        if freqmode==FREQMODE.normalized or freqmode=='normalized':
            self.freqmode=FREQMODE.normalized
        elif freqmode==FREQMODE.basebandfs or freqmode=='basebandfs':
            self.freqmode=FREQMODE.basebandfs
            self.get_freq=lambda f: f*fs
            self.set_freq=lambda f: f/fs
            self.get_bw=lambda bw: bw/fs
            self.set_bw=lambda bw: bw/fs
            
        elif freqmode==FREQMODE.centerfreqfs or freqmode=='centerfreqfs':
            self.freqmode=FREQMODE.centerfreqfs
            self.get_freq=lambda f: f*fs + centerfreq -fs/2
            self.set_freq=lambda f: (f-centerfreq+fs/2)/fs
            self.get_bw=lambda bw: bw/fs
            self.set_bw=lambda bw: bw/fs
        else:
            raise ValueError('Unknown Frequency mode. Exiting...')
        
        #get all throughput channels
        self.throughput_channels=[]
        if throughput_channels is None:
            pass
        elif isinstance(throughput_channels,(list,tuple)):
            self.throughput_channels=[]
            for k in throughput_channels:
                c=self.get_channel(k)
                if c is None:
                    raise ValueError('Cannot convert {} to channel. must be list or tuple with channel frequency and bandwidth. '.format(k))
                self.throughput_channels.append(c)
        else:
            raise ValueError('Throughput channels are invalid. Exiting...')
        
        #get all activity controlled channels
        self.activity_controlled_channels=[]
        if activity_controlled_channels is None:
            pass
        elif isinstance(activity_controlled_channels,(list,tuple)):
            self.activity_controlled_channels=[]
            for k in activity_controlled_channels:
                c=self.get_channel(k)
                if c is None:
                    raise ValueError('Cannot convert {} to channel. must be list or tuple with channel frequency and bandwidth. '.format(k))
                self.activity_controlled_channels.append(c)
        else:
            raise ValueError('Activity controlled channels are invalid. Exiting...')
            
        
        #get all activity detection segments
        self.activity_detection_segments=[]
        if activity_detection_segments is None:
            pass
        elif isinstance(activity_detection_segments,(list,tuple)):
            self.activity_detection_segments=[]
            for k in activity_detection_segments:
                c=self.get_segment(k)
                if c is None:
                    raise ValueError('Cannot convert {} to segment. must be list or tuple with channel start and stop frequency. '.format(k))
                self.activity_detection_segments.append(c)
        else:
            raise ValueError('Activity detection segments are invalid. Exiting...')
        
        
        self.inpveclen=int(inpveclen) if int(inpveclen)>0 else 1
        self.blocksize=nextpow2(blocksize)
        self.relinvovl=nextpow2(relinvovl)
        self.ovllen=self.blocksize // self.relinvovl
        self.inpblocklen=self.blocksize-self.ovllen
        
        
        #define output stream signature depending on throughput channels and debug output. 
        outsig=gr.io_signature(0, 0, 0)
        
        gr.hier_block2.__init__(self,
                                "FreqDomChan",
                                gr.io_signature_make(1, 1, self.itemsize * self.inpveclen),
                                outsig)
        
        if len(self.throughput_channels)>0:
            if self.debug:
                outsig=gr.io_signature_make2(len(self.throughput_channels)+1, len(self.throughput_channels)+1, gr.sizeof_gr_complex*self.blocksize, gr.sizeof_gr_complex)
            else:
                outsig=gr.io_signature_make(len(self.throughput_channels), len(self.throughput_channels), gr.sizeof_gr_complex)
        elif self.debug:
            outsig=gr.io_signature_make(1, 1, gr.sizeof_gr_complex*self.blocksize)
        
        gr.hier_block2.__init__(self,
                                "FreqDomChan",
                                gr.io_signature_make(1, 1, self.itemsize * self.inpveclen),
                                outsig)
        
        #register message port if used
        if(msgoutput):
            self.msgport="msgout"
            self.message_port_register_hier_out(self.msgport)
                                    
                                    
        
        
        
        
        #debug info
        if(self.verbose):
            self.log('\n' + '#'*32 + '\n')
            self.log('# gr-FDC Frequency Domain Channelizer Runtime Information')
            self.log('\n' + '#'*32 + '\n')
            self.log('Blocksize     = {}'.format(self.blocksize))
            self.log('InputVecLen   = {}'.format(self.inpveclen))
            self.log('Relinvovl     = {}'.format(self.relinvovl))
            self.log('Ovllen        = {}'.format(self.ovllen))
            self.log('MsgOutput     = {}'.format(msgoutput))
            self.log('FileOutput    = {}'.format(fileoutput))
            self.log('Outputpath    = {}'.format(outputpath))
            self.log('Threaded      = {}'.format(threaded))
            self.log('Debugoutput   = {}'.format(self.debug))
            self.log('\n' + '#'*32 + '\n')
            self.log('# Throughput channels:         {}'.format(str(self.throughput_channels)))
            self.log('# Activity control channels:   {}'.format(str(self.activity_controlled_channels)))
            self.log('# Activity detection segments: {}'.format(str(self.activity_detection_segments)))
            self.log('\n' + '#'*32 + '\n')
            
        
        
        
        
        
        #define blocks
        if self.inpveclen==1:
            self.inp_block_distr = blocks.stream_to_vector(self.itemsize, self.inpblocklen )
            self.overlap_save = overlap_save(self.itemsize, self.blocksize, self.ovllen)
            self.fft = None
            if self.itemsize == gr.sizeof_gr_complex:
                self.fft = fft.fft_vcc(self.blocksize, True, (fft.window.rectangular(self.blocksize)), False, 4)
            elif self.itemsize == gr.sizeof_gr_complex:
                self.fft = fft.fft_vfc(self.blocksize, True, (fft.window.rectangular(self.blocksize)), False, 4)
            else:
                raise ValueError('Unknown input type. ')
        
        
        if self.itemsize==gr.sizeof_float:
            self.normalize_input = blocks.multiply_const_ff( 1.0/float(self.blocksize), self.blocksize)
        else:
            self.normalize_input = blocks.multiply_const_cc( 1.0/float(self.blocksize), self.blocksize)
        
        self.throughput_channelizers=[ [None]*5 for i in range(len(self.throughput_channels)) ]
        for i, (freq, bw) in enumerate(self.throughput_channels):
            f,l,lout,pbw,sbw = self.get_opt_channelparams(freq,bw)
            
            if self.verbose:
                self.log('# Throughput Channel {}: f={}, l={}, lout={}, bw=({}, {})'.format(i,f,l,lout,sbw,pbw))
            
            self.throughput_channelizers[i][0] = vector_cut_vxx(gr.sizeof_gr_complex, self.blocksize, f, l )
            self.throughput_channelizers[i][1] = phase_shifting_windowing_vcc(l, self.relinvovl, f, pbw, sbw, windowtype)
            self.throughput_channelizers[i][2] = fft.fft_vcc(l, False, (fft.window.rectangular(l)), True, 1)
            self.throughput_channelizers[i][3] = vector_cut_vxx(gr.sizeof_gr_complex, l, l-lout, lout )
            self.throughput_channelizers[i][4] = blocks.vector_to_stream(gr.sizeof_gr_complex, lout)
        
        self.N_throughput_channelizers=len(self.throughput_channelizers)
        
        
        #create activity controlled channelizer if valid channels are present
        if len(self.activity_controlled_channels):
            self.PowerActChans=[None]*len(self.activity_controlled_channels)
            for i,(cfreq, bw) in enumerate(self.activity_controlled_channels):
                self.PowerActChans[i]=PowerActivationChannel(self.blocksize, 
                                                             cfreq, 
                                                             bw, 
                                                             self.relinvovl, 
                                                             float(act_contr_threshold),
                                                             int(pow_act_maxblocks),
                                                             int(pow_act_deactivation_delay) if int(pow_act_deactivation_delay)>=0 else 0,
                                                             bool(msgoutput),
                                                             bool(fileoutput),
                                                             str(outputpath), 
                                                             self.verbose,
                                                             i)
           #int v_blocklen, float v_cfreq, float v_bw, int v_relinvovl, float v_thresh, int v_maxblocks, 
           #int v_deactivation_delay, bool v_msg, bool v_fileoutput, std::string v_path, int verbose, int v_ID
        
        
            
        
        
        
        #create activity detection channelizer if valid segments are present
        
        if len(self.activity_detection_segments):
            self.activity_detection_channelizer=activity_detection_channelizer_vcm(self.blocksize, 
                                                                                   self.activity_detection_segments, 
                                                                                   float(act_det_threshold), 
                                                                                   self.relinvovl, 
                                                                                   int(act_det_maxblocks), 
                                                                                   bool(msgoutput), 
                                                                                   bool(fileoutput), 
                                                                                   str(outputpath), 
                                                                                   bool(threaded),
                                                                                   self.get_bw(minchandist),
                                                                                   int(act_det_deactivation_delay) if int(act_det_deactivation_delay)>=0 else 0,
                                                                                   float(minchanflankpuffer) if 0.0<=float(minchanflankpuffer) else 0.2,
                                                                                   self.verbose)
            #int v_blocklen, std::vector< std::vector< float > > v_segments, float v_thresh, int v_relinvovl, 
            #int v_maxblocks, bool v_message, bool v_fileoutput, std::string v_path, bool v_threads, float v_minchandist, 
            #int v_channel_deactivation_delay, double v_window_flank_puffer
            
            
        
        
        #define connections
        if self.inpveclen==1:
            self.connect( (self,0), (self.inp_block_distr,0) )
            self.connect( (self.inp_block_distr,0), (self.overlap_save,0) )
            self.connect( (self.overlap_save,0), (self.fft,0) )
            self.connect( (self.fft,0), (self.normalize_input, 0) )
        else:
            self.connect( (self,0), (self.normalize_input, 0) )
        
        for i in list(range(self.N_throughput_channelizers)):
            self.connect( (self.normalize_input,0), (self.throughput_channelizers[i][0], 0) )
            self.connect( (self.throughput_channelizers[i][0], 0), (self.throughput_channelizers[i][1], 0) )
            self.connect( (self.throughput_channelizers[i][1], 0), (self.throughput_channelizers[i][2], 0) )
            self.connect( (self.throughput_channelizers[i][2], 0), (self.throughput_channelizers[i][3], 0) )
            self.connect( (self.throughput_channelizers[i][3], 0), (self.throughput_channelizers[i][4], 0) )
            self.connect( (self.throughput_channelizers[i][4], 0), (self, i + int(self.debug)) )
    
        if len(self.activity_controlled_channels):
            for i in range(len(self.PowerActChans)):
                self.connect( (self.normalize_input, 0), (self.PowerActChans[i], 0) )
                if msgoutput:
                    self.msg_connect( self.PowerActChans[i], pmt.intern("msgout"), self, pmt.intern(self.msgport) )
            
        
        if len(self.activity_detection_segments):
            self.connect( (self.normalize_input, 0), (self.activity_detection_channelizer, 0) )
            if msgoutput:
                self.msg_connect( self.activity_detection_channelizer, pmt.intern("msgout"), self, pmt.intern(self.msgport) )
        
        if self.debug:
            self.connect( (self.normalize_input,0), (self, 0 ) )
        
        
    
    
    
    
    def get_opt_channelparams(self, freq, bw):
        passsamps=self.blocksize * bw
        blocklen=nextpow2(passsamps)
        
        if blocklen < 1.2 * passsamps: #20% puffer
            blocklen*=2
        
        passband=float(passsamps) / float(blocklen) * 1.1
        stopband=1.0
        if passband >=1.0:
            passband=1.0
        elif passband < 0.7:
            stopband=passband+0.25
        
        freqsamps=int(round(freq*self.blocksize)) % self.blocksize
        if freqsamps<0:
            freqsamps=(freqsamps + self.blocksize) % self.blocksize
        if freqsamps+blocklen > self.blocksize:
            freqsamps=self.blocksize-blocklen
            
        outputblocklen=int(blocklen) - int(blocklen)//self.relinvovl
        
        return int(freqsamps), int(blocklen), int(outputblocklen), float(passband), float(stopband)
            
        
    
    def get_channel(self,c):
        if not isinstance(c, (list, tuple)) or len(c)!=2:
            return None
        return [self.get_freq(c[0]), self.get_bw(c[1])]
    
    def get_segment(self,c):
        if not isinstance(c, (list, tuple)) or len(c)!=2:
            return None
        return [self.get_freq(c[0]), self.get_freq(c[1])]
    
    def log(self, s):
        if self.verbose==VERBOSEMODE.LOGTOCONSOLE:
            print(str(s))
        elif self.verbose==VERBOSEMODE.LOGTOFILE:
            self.logtofile(s)
    
    def logtofile(self, s, end='\n'):
        if not hasattr( self, 'logfile' ):
            self.logfile='gr-FDC.FreqDomChan.log'
            with open(self.logfile,'w') as fh:
                fh.write('\n')
        with open( self.logfile, 'a' ) as fh:
            fh.write( str(s)+str(end) )
