"""
Simple class which encapsulate an APT controller
"""
import pylibftdi
import time
import struct as st

from message import Message
import message

pylibftdi.USB_PID_LIST+=[0xfaf0]

class Controller(object):
  def __init__(self, serial_number=None, label=None):
    super(Controller, self).__init__()

    # this takes up to 2-3s:
    dev = pylibftdi.Device(mode='b', device_id=serial_number)
    dev.baudrate = 115200

    def _checked_c(ret):
      if not ret == 0:
        raise Exception(dev.ftdi_fn.ftdi_get_error_string())

    _checked_c(dev.ftdi_fn.ftdi_set_line_property(  8,   # number of bits
                                                    1,   # number of stop bits
                                                    0   # no parity
                                                    ))
    time.sleep(50.0/1000)

    dev.flush(pylibftdi.FLUSH_BOTH)

    time.sleep(50.0/1000)

    # skipping reset part since it looks like pylibftdi does it already

    # this is pulled from ftdi.h
    SIO_RTS_CTS_HS = (0x1 << 8)
    _checked_c(dev.ftdi_fn.ftdi_setflowctrl(SIO_RTS_CTS_HS))

    _checked_c(dev.ftdi_fn.ftdi_setrts(1))

    self.serial_number = serial_number
    self.label = label
    self._device = dev

    # some sane limits, mostly based on the MTS50/M-Z8
    # velocity is in mm/s
    # acceleration is in mm^2/s
    self.max_velocity = 3
    self.max_acceleration = 4

    # these define how encode count translates into position, velocity
    # and acceleration. e.g. 1 encoder count is equal to 
    # 1 * self.position_scale mm 
    self.velocity_scale = 767367.49
    self.position_scale = 34304
    self.acceleration_scale = 261.93

    # defines the linear, i.e. distance, range of the controller
    # unit is in mm
    self.linear_range = (0,50)

    # the message queue are messages that are sent asynchronously. For example
    # if we performed a move, and are waiting for move completed message,
    # any other message received in the mean time are place in the queue.
    self.message_queue = []

  def __enter__(self):
    return self

  def __exit__(self, type_, value, traceback):
    self._device.close()

  def _send_message(self, m):
    """
    m should be an instance of Message, or has a pack() method which returns
    bytes to be sent to the controller
    """
    self._device.write(m.pack())

  def _read(self, length, block=True):
    """
    If block is True, then we will return only when have have length number of
    bytes. Otherwise we will perform a read, then immediately return with
    however many bytes we managed to read.

    Note that if no data is available, then an empty byte string will be 
    returned.
    """
    data = bytes()
    while len(data) < length:
      diff = length - len(data)
      data += self._device.read(diff)
      if not block:
        break
      time.sleep(0.001)

    return data

  def _read_message(self):
    data = self._read(message.MGMSG_HEADER_SIZE)
    msg = Message.unpack(data, header_only=True)
    if msg.hasdata:
      data = self._read(msg.datalength)
      msglist = list(msg)
      msglist[-1] = data
      return Message._make(msglist)
    return msg

  def _wait_message(self, expected_messageID):
    found = False
    while not found:
      m = self._read_message()
      found = m.messageID == expected_messageID
      if found:
        return m
      else:
        self.message_queue.append(m)

  def _decode_status(self,statusbits):
    """
    Decodes the statusbits returned by the controller as part of move completed
    and other messages. Returns a list of strings corresponding to set flags.
    """
    masks={ 0x01:       'Forward hardware limit switch active',
            0x02:       'Reverse hardware limit switch active',
            0x10:       'In motion, moving forward',
            0x20:       'In motion, moving backward',
            0x40:       'In motion, jogging forward',
            0x80:       'In motion, jogging backward',
            0x200:      'In motion, homing',
            0x400:      'Homed',
            0x1000:     'Tracking',
            0x2000:     'Settled',
            0x4000:     'Motion error, excessive position',
            0x01000000: 'Motor current limit reached',
            0x80000000: 'Channel enabled'
            }
    statuslist = []
    for bitmask in masks:
      if statusbits & bitmask:
        statuslist.append(masks[bitmask])

    return statuslist

  def status(self, channel=0):
    """
    Returns the status of the controller, which is its position, velocity, and
    a list of status strings.

    Position and velocity will be in mm and mm/s respectively.
    """
    reqmsg = Message(message.MGMSG_MOT_REQ_DCSTATUSUPDATE, param1=channel)
    self._send_message(reqmsg)

    getmsg = self._wait_message(message.MGMSG_MOT_GET_DCSTATUSUPDATE)

    """
    <: little endian
    H: 2 bytes for channel ID
    i: 4 bytes for position counter
    H: 2 bytes for velocity
    H: 2 bytes for reserved
    I: 4 bytes for status
    """
    channel, pos_apt, vel_apt, x, status = st.unpack('<HiHHI',getmsg.datastring)
    return (pos_apt/self.position_scale,
            vel_apt/self.velocity_scale,
            self._decode_status(status))

  def identify(self):
    """
    Flashes the controller's activity LED
    """
    idmsg = Message(message.MGMSG_MOD_IDENTIFY)
    self._send_message(idmsg)

  def reset_params(self):
    resetmsg = Message(message.MGMSG_MOT_SET_PZSTAGEPARAMDEFAULTS)
    self._send_message(resetmsg)

  def request_home_params(self):
    reqmsg = Message(message.MGMSG_MOT_REQ_HOMEPARAMS)
    self._send_message(reqmsg)

    getmsg = self._wait_message(message.MGMSG_MOT_GET_HOMEPARAMS)
    dstr = getmsg.datastring

    """
    <: little endian
    H: 2 bytes for channel id
    H: 2 bytes for home direction
    H: 2 bytes for limit switch
    i: 4 bytes for homing velocity
    i: 4 bytes for offset distance
    """
    return st.unpack('<HHHii', dstr)

  def home(self, wait=True, velocity=0):
    """
    When velocity is not 0, homing parameters will be set so homing velocity
    will be as given, in mm per second.

    When wait is true, this method doesn't return until MGMSG_MOT_MOVE_HOMED
    is received. Otherwise it returns immediately after having sent the
    message.
    """

    if velocity > 0:
      # first get the current settings for homing. We do this because despite
      # the fact that the protocol spec says home direction, limit switch,
      # and offet distance parameters are not used, they are in fact 
      # significant. If I just pass in 0s for those parameters when setting
      # homing parameter the stage goes the wrong way and runs itself into
      # the other end, causing an error condition.
      #
      # To get around this, and the fact the correct values don't seem to be
      # documented, we get the current parameters, assuming they are correct,
      # and then modify only the velocity component, then send it back to the
      # controller.
      curparams = list(self.request_home_params())

      # make sure we never exceed the limits of our stage
      velocity = min(self.max_velocity, velocity)

      """
      <: little endian
      H: 2 bytes for channel id
      H: 2 bytes for home direction
      H: 2 bytes for limit switch
      i: 4 bytes for homing velocity
      i: 4 bytes for offset distance
      """

      curparams[-2] = int(velocity*self.velocity_scale)
      newparams= st.pack( '<HHHii',*curparams)

      homeparamsmsg = Message(message.MGMSG_MOT_SET_HOMEPARAMS, data=newparams)
      self._send_message(homeparamsmsg)

    homemsg = Message(message.MGMSG_MOT_MOVE_HOME)
    self._send_message(homemsg)

    if wait:
      m = self._wait_message(message.MGMSG_MOT_MOVE_HOMED)

  def position(self, channel=0):
    reqmsg = Message(message.MGMSG_MOT_REQ_POSCOUNTER, param1=channel)
    self._send_message(reqmsg)

    getmsg = self._wait_message(message.MGMSG_MOT_GET_POSCOUNTER)
    dstr = getmsg.datastring

    """
    <: little endian
    H: 2 bytes for channel id
    i: 4 bytes for position
    """
    chanid,pos_apt=st.unpack('<Hi', dstr)

    # convert position from POS_apt to POS using _position_scale
    return pos_apt / self.position_scale

  def goto(self, abs_pos_mm, channel=0, wait=True):
    """
    Tells the stage to goto the specified absolute position, in mm.

    abs_pos_mm will be clamped to self.linear_range

    When wait is True, this method only returns when the stage has signaled
    that it has finished moving.
    """

    # do some software limiting for extra safety
    abs_pos_mm = min(abs_pos_mm, self.linear_range[1])
    abs_pos_mm = max(abs_pos_mm, self.linear_range[0])

    abs_pos_apt = int(abs_pos_mm * self.position_scale)

    """
    <: little endian
    H: 2 bytes for channel id
    i: 4 bytes for absolute position
    """
    params = st.pack( '<Hi', channel, abs_pos_apt)

    movemsg = Message(message.MGMSG_MOT_MOVE_ABSOLUTE,data=params)
    self._send_message(movemsg)

    if wait:
      msg = self._wait_message(message.MGMSG_MOT_MOVE_COMPLETED)


  def move(self, dist_mm, channel=0, wait=True):
    """
    Tells the stage to move from its current position the specified
    distance, in mm
    """
    curpos = self.position()
    newpos = curpos + dist_mm

    # like move, we to software limiting for safety
    newpos = min(newpos, self.linear_range[1])
    newpos = max(newpos, self.linear_range[0])

    # We could implement MGMSG_MOT_MOVE_RELATIVE, or we can use goto again
    # because we calculate the new absolute position anyway.
    #
    # The advantage of implementing MGMSG_MOT_MOVE_RELATIVE is that things
    # will be a little faster, because we don't need to get the current
    # position first. The advantage of reusing self.goto is that it is easier
    # to implement initially.
    #
    # Of course by the time I have finished writing this comment, I could have
    # just implemented MGMSG_MOT_MOVE_RELATIVE.
    self.goto(newpos, channel=channel, wait=wait)

class MTS50Controller(Controller):
  """
  A controller for a MTS50/M-Z8 stage.
  """
  def __init__(self,*args, **kwargs):
    super(MTS50Controller, self).__init__(*args, **kwargs)

    # http://www.thorlabs.co.uk/thorProduct.cfm?partNumber=MTS50/M-Z8
    self.max_velocity = 3
    self.max_acceleration = 4.5

    self.position_scale = 34304
    self.velocity_scale = 767367.49
    self.acceleration_scale = 261.93

    self.linear_range = (0,50)

