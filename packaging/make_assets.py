"""Generate the existing vector-style Windows icon and a generic install guide."""
from pathlib import Path
from PIL import Image,ImageDraw

ROOT=Path(__file__).resolve().parents[1]
image=Image.new('RGBA',(512,512),(0,0,0,0)); draw=ImageDraw.Draw(image)
draw.rounded_rectangle((0,0,511,511),radius=128,fill='#168879')
draw.line([(128,360),(360,128)],fill='white',width=56)
draw.line([(200,128),(360,128),(360,288)],fill='white',width=56,joint='curve')
for x,y in [(128,360),(200,128),(360,128),(360,288)]: draw.ellipse((x-28,y-28,x+28,y+28),fill='white')
image.save(ROOT/'packaging'/'app.ico',sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])

def guide():
    from docx import Document
    from docx.shared import Inches,Pt,RGBColor
    from docx.oxml.ns import qn
    doc=Document();s=doc.sections[0]
    s.page_width=Inches(8.5);s.page_height=Inches(11);s.top_margin=Inches(.8);s.bottom_margin=Inches(.8);s.left_margin=Inches(.85);s.right_margin=Inches(.85)
    for name,size in [('Normal',11),('Title',24),('Heading 1',15)]:
        style=doc.styles[name];style.font.name='Microsoft YaHei';style.font.size=Pt(size);style.font.color.rgb=RGBColor(0,0,0)
        style._element.get_or_add_rPr().rFonts.set(qn('w:eastAsia'),'Microsoft YaHei')
        style.paragraph_format.space_after=Pt(9);style.paragraph_format.line_spacing=1.15
    doc.add_paragraph('量研 Windows安装版操作指南',style='Title')
    doc.add_paragraph('本指南介绍安装、启动、查看模拟结果和保护本地数据。软件先使用虚拟资金模拟；实际下单始终由你在券商软件完成。')
    entries=[
    ('安装与打开','双击“量研安装程序”EXE，选择程序目录和数据目录，完成安装。之后双击桌面“量研”图标即可打开独立窗口，不需要安装Python。支持Windows 10和11的64位系统。'),
    ('开始工作','点击“开始工作”。两套模拟账户各有22,000元，切换首页的稳健或激进只切换显示，两套都会继续模拟。'),
    ('等待数据与查看计划','首次使用要下载全市场历史。在“数据与设置”看总百分比；失败时展开错误并点“重试失败数据”。数据齐全后，首页有机会才显示买入范围、建议股数和退出价格。'),
    ('查看模拟结果','打开“模拟表现”看账户金额及已经完成的买卖。过去行情检验和后续模拟需要时间，数据不足时保持等待，不会因为运行几天就自动启用实盘。'),
    ('记录真实账户','新电脑默认真实账户为空。转入或取出资金后，在“真实持仓”展开“核对可用现金”；实际在券商买卖完成后，再展开“记录我已完成的买卖”。保存只是记账，不是下单。已有账户可在程序彻底退出后整体迁移原数据目录，迁移前先备份。'),
    ('关闭与退出','关闭窗口会隐藏到右下角托盘，后台继续工作。再次双击桌面图标，或双击托盘图标可打开窗口。点“停止工作”暂停；托盘右键“退出软件”才彻底退出。关机后停止，下次打开再点开始工作。'),
    ('数据与卸载','数据目录在安装时选择，并显示在“数据与设置”中。已有本机数据不需要重新下载。卸载保留行情和账户数据；复制整个数据目录前先退出软件。正常网络即可，无需梯子；本地运行不消耗Codex额度。'),
    ('使用范围','软件没有承诺盈利。报价过期、断网和休市时暂停模拟成交；停止条件也不能保证在跳空、停牌等情况下按预定价格成交。')]
    for title,body in entries:doc.add_heading(title,1);doc.add_paragraph(body)
    for node in list(doc._element.iter(qn('w:pBdr'))):node.getparent().remove(node)
    for node in list(doc.styles._element.iter(qn('w:pBdr'))):node.getparent().remove(node)
    doc.core_properties.title='量研 Windows安装版操作指南';doc.core_properties.author='量研项目'
    doc.save(ROOT/'packaging'/'guide.docx')

if __name__=='__main__': guide()
