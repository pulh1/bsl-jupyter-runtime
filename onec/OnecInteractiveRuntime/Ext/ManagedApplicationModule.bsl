&После("ПриНачалеРаботыСистемы")
Процедура OnecInteractiveRuntime_ПриНачалеРаботыСистемы()
	ИдентификаторПродуктаRuntime = "onec-interactive-runtime";
	ВерсияАртефактаRuntime = "0.1.2";
	ВерсияПротоколаRuntime = "1";
	ПродолжатьЦикл = Ложь;
	С = 1; // @runtime-extension-service-breakpoint
	Если ПродолжатьЦикл Тогда
		RuntimeKernelServer.Запустить();
	КонецЕсли;
КонецПроцедуры
